import json
import traceback
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from e2b import CommandExitException

from providers.e2b.api import E2BAPI
from providers.e2b.provider import E2BProvider
from providers.e2b.settings import E2BSettings
from providers.exceptions import ProviderAuthError, ProviderCommandError, ProviderNotFoundError
from providers.registry import get_provider_names

BASE = "https://api.e2b.app"
RUN = "e2b.sandbox_async.commands.command.Commands.run"
SANDBOX = {
    "sandboxID": "sandbox-1",
    "templateID": "template-1",
    "clientID": "client-1",
    "envdVersion": "0.4.0",
}
LISTED = {
    **SANDBOX,
    "startedAt": "2026-10-04T12:00:00Z",
    "endAt": "2026-10-04T13:00:00Z",
    "cpuCount": 2,
    "memoryMB": 1024,
    "diskSizeMB": 1024,
    "state": "running",
}


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> E2BProvider:
    # The SDK's own transport bypasses respx.
    monkeypatch.setattr(
        "e2b.api.client_async.get_transport",
        lambda *args, **kwargs: httpx.MockTransport(respx.mock.async_handler),
    )
    settings = E2BSettings(api_key="e2b_test_key", default_image="drukbox:v1")
    return E2BProvider(E2BAPI(settings), settings, service_label="drukbox")


@respx.mock
async def test_create_bootstraps_sandbox_on_the_tailnet(provider: E2BProvider):
    creation = respx.post(BASE + "/v2/sandboxes").respond(201, json=SANDBOX)
    with patch(RUN, new_callable=AsyncMock) as run:
        result = await provider.create_vm(
            name="sb-test", image="custom:v2", env={"FOO": "hello world"}, setup_script="true"
        )
    request = creation.calls[0].request
    body = json.loads(request.content)
    assert request.headers["X-API-Key"] == "e2b_test_key"
    assert body["templateID"] == "custom:v2"
    assert body["timeout"] == 3600
    assert body["metadata"] == {"name": "sb-test", "managed-by": "drukbox"}
    assert body["autoPause"] is False
    assert body["network"]["allowPublicTraffic"] is False
    assert run.call_args.kwargs["user"] == "root"
    assert run.call_args.args[0].startswith("ip link set dummy0 up")
    assert "hello world" in run.call_args.args[0]
    assert result.provider_id == "sandbox-1"
    assert result.ssh_username == "root"
    assert provider.max_lifetime == timedelta(hours=1)


@respx.mock
async def test_rejected_create_names_e2b_reason(provider: E2BProvider):
    respx.post(BASE + "/v2/sandboxes").respond(
        400, json={"code": 400, "message": "template 'missing' not found"}
    )
    with pytest.raises(ProviderCommandError, match="template 'missing' not found"):
        await provider.create_vm(name="sb-test", image="missing", setup_script="true")


@respx.mock
async def test_failed_bootstrap_hides_output(provider: E2BProvider):
    respx.post(BASE + "/v2/sandboxes").respond(201, json=SANDBOX)
    failure = CommandExitException(stderr="secret", stdout="secret", exit_code=1, error=None)
    with (
        patch(RUN, new_callable=AsyncMock, side_effect=failure),
        pytest.raises(ProviderCommandError) as error,
    ):
        await provider.create_vm(name="sb-test", image="drukbox:v1", setup_script="true")
    assert "secret" not in "".join(traceback.format_exception(error.value))


@respx.mock
async def test_delete_kills_the_named_sandbox(provider: E2BProvider):
    listing = respx.get(BASE + "/v2/sandboxes").respond(200, json=[LISTED])
    kill = respx.delete(BASE + "/sandboxes/sandbox-1").respond(204)
    await provider.delete_vm("sb-test")
    assert "managed-by" in listing.calls[0].request.url.params["metadata"]
    assert kill.called
    listing.respond(200, json=[])
    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")


@respx.mock
async def test_diagnose_checks_authentication(provider: E2BProvider):
    listing = respx.get(BASE + "/v2/sandboxes").respond(200, json=[])
    assert "authentication succeeded" in await provider.diagnose()
    listing.respond(401, json={"code": 401, "message": "invalid key"})
    with pytest.raises(ProviderAuthError):
        await provider.diagnose()


def test_provider_is_registered():
    assert "e2b" in get_provider_names()
