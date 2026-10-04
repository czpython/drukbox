import asyncio
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
from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from providers.registry import get_provider_names

BASE = "https://api.e2b.app"
COMMAND = "e2b.sandbox_async.commands.command.Commands.run"
SANDBOX = {
    "sandboxID": "sandbox-1",
    "templateID": "template-1",
    "clientID": "client-1",
    "envdVersion": "0.4.0",
    "envdAccessToken": "envd-test-token",
}
LISTED = {
    **SANDBOX,
    "startedAt": "2026-10-04T12:00:00Z",
    "endAt": "2026-10-04T13:00:00Z",
    "cpuCount": 2,
    "memoryMB": 1024,
    "diskSizeMB": 1024,
    "state": "running",
    "metadata": {"name": "sb-test", "managed-by": "drukbox"},
}


@pytest.fixture
def sdk_transport(monkeypatch: pytest.MonkeyPatch):
    # The SDK's Rust transport bypasses respx's httpcore interception.
    monkeypatch.setattr(
        "e2b.api.client_async.get_transport",
        lambda *args, **kwargs: httpx.MockTransport(respx.mock.async_handler),
    )


@pytest.fixture
def provider(sdk_transport) -> E2BProvider:
    settings = E2BSettings(api_key="e2b_test_key", default_image="drukbox:v1")
    return E2BProvider(E2BAPI(settings), settings, service_label="drukbox")


@respx.mock
async def test_create_uses_template_and_enforces_fixed_lifetime(provider: E2BProvider):
    creation = respx.post(BASE + "/v2/sandboxes").respond(201, json=SANDBOX)
    with patch(COMMAND, new_callable=AsyncMock) as command:
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
    assert body["autoResume"] == {"enabled": False}
    assert body["network"]["allowPublicTraffic"] is False
    assert command.call_args.kwargs["user"] == "root"
    assert command.call_args.kwargs["timeout"] == 120
    assert "/etc/environment" in command.call_args.args[0]
    assert "hello world" in command.call_args.args[0]
    assert result.provider_id == "sandbox-1"
    assert result.name == "sb-test"
    assert result.ssh_username == "root"
    assert not result.ssh_host
    assert provider.max_lifetime == timedelta(seconds=3540)


@respx.mock
async def test_no_tailscale_or_invalid_environment_makes_no_request(provider: E2BProvider):
    with pytest.raises(ProviderCommandError, match="TAILSCALE_ENABLED"):
        await provider.create_vm(name="sb-test", image="template")
    with pytest.raises(ProviderCommandError, match="environment"):
        await provider.create_vm(
            name="sb-test", image="template", setup_script="true", env={"BAD-KEY": "value"}
        )
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_rejected_creation_does_not_delete(provider: E2BProvider, status: int):
    creation = respx.post(BASE + "/v2/sandboxes").respond(
        status, json={"code": status, "message": "sensitive details"}
    )
    with pytest.raises(ProviderCommandError) as error:
        await provider.create_vm(name="sb-test", image="template", setup_script="true")
    assert "sensitive" not in str(error.value)
    assert creation.call_count == 1
    assert len(respx.calls) == 1


@respx.mock
async def test_lost_create_response_is_not_retried_and_owned_vm_is_deleted(provider: E2BProvider):
    creation = respx.post(BASE + "/v2/sandboxes").mock(side_effect=httpx.ReadTimeout("lost"))
    respx.get(BASE + "/v2/sandboxes").respond(200, json=[LISTED])
    deletion = respx.delete(BASE + "/sandboxes/sandbox-1").respond(204)
    with pytest.raises(ProviderTransportError):
        await provider.create_vm(name="sb-test", image="template", setup_script="true")
    assert creation.call_count == 1
    assert deletion.called


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        CommandExitException(stderr="secret", stdout="secret", exit_code=1, error=None),
        asyncio.CancelledError(),
    ],
)
async def test_failed_or_cancelled_bootstrap_cleans_up(
    provider: E2BProvider, failure: BaseException
):
    respx.post(BASE + "/v2/sandboxes").respond(201, json=SANDBOX)
    respx.get(BASE + "/v2/sandboxes").respond(200, json=[LISTED])
    deletion = respx.delete(BASE + "/sandboxes/sandbox-1").respond(204)
    with (
        patch(COMMAND, new_callable=AsyncMock, side_effect=failure),
        pytest.raises((ProviderTransportError, asyncio.CancelledError)) as error,
    ):
        await provider.create_vm(name="sb-test", image="template", setup_script="true")
    assert "secret" not in "".join(traceback.format_exception(error.value))
    assert deletion.called


@respx.mock
async def test_delete_collects_all_pages_before_deleting_and_checks_ownership(
    provider: E2BProvider,
):
    listing = respx.get(BASE + "/v2/sandboxes").mock(
        side_effect=[
            httpx.Response(200, json=[LISTED], headers={"X-Next-Token": "page-2"}),
            httpx.Response(
                200,
                json=[
                    {**LISTED, "sandboxID": "sandbox-2", "state": "paused"},
                    {**LISTED, "sandboxID": "foreign", "metadata": {"managed-by": "other"}},
                ],
            ),
        ]
    )
    first = respx.delete(BASE + "/sandboxes/sandbox-1").respond(204)
    second = respx.delete(BASE + "/sandboxes/sandbox-2").respond(
        404, json={"code": 404, "message": "gone"}
    )
    await provider.delete_vm("sb-test")
    assert listing.call_count == 2
    assert listing.calls[1].request.url.params["nextToken"] == "page-2"
    assert "managed-by" in listing.calls[0].request.url.params["metadata"]
    assert listing.calls[0].request.url.params["state"] == "running,paused"
    assert first.called and second.called
    assert [call.request.method for call in respx.calls] == ["GET", "GET", "DELETE", "DELETE"]


@respx.mock
async def test_missing_sandbox_raises_not_found(provider: E2BProvider):
    respx.get(BASE + "/v2/sandboxes").respond(200, json=[])
    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")


@respx.mock
async def test_diagnose_authenticates_with_one_read(provider: E2BProvider):
    listing = respx.get(BASE + "/v2/sandboxes").respond(200, json=[])
    assert "authentication succeeded" in await provider.diagnose()
    assert listing.calls[0].request.url.params["limit"] == "1"
    listing.respond(401, json={"code": 401, "message": "secret"})
    with pytest.raises(ProviderAuthError, match="authentication failed"):
        await provider.diagnose()


def test_provider_registration_and_lifetime_settings():
    assert "e2b" in get_provider_names()
    for timeout in [599, 86401]:
        with pytest.raises(ValueError):
            E2BSettings(api_key="test", default_image="template", session_timeout_seconds=timeout)
