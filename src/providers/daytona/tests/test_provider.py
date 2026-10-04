import asyncio
import json
import traceback
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from providers.daytona.api import DaytonaAPI
from providers.daytona.provider import DaytonaProvider
from providers.daytona.settings import DaytonaSettings
from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from providers.registry import get_provider_names

BASE = "https://app.daytona.io/api/"
TOOLBOX = "https://proxy.app.daytona.io/toolbox"
COMMAND = TOOLBOX + "/sandbox-1/process/execute"
SNAPSHOT = {"sandboxClass": "linux-vm", "state": "active"}
SANDBOX = {
    "id": "sandbox-1",
    "name": "sb-test",
    "labels": {"managed-by": "drukbox"},
    "state": "started",
    "sandboxClass": "linux-vm",
    "autoStopInterval": 0,
    "autoPauseInterval": 0,
    "autoDeleteInterval": -1,
    "toolboxProxyUrl": TOOLBOX,
}


@pytest.fixture
async def provider() -> AsyncIterator[DaytonaProvider]:
    settings = DaytonaSettings(api_key="test-key", default_image="drukbox-vm", target="eu")
    provider = DaytonaProvider(DaytonaAPI(settings), settings, service_label="drukbox")
    yield provider
    await provider.aclose()


@respx.mock
async def test_create_uses_linux_vm_snapshot_and_bootstraps_tailnet(provider: DaytonaProvider):
    snapshot = respx.get(BASE + "snapshots/custom-vm").respond(200, json=SNAPSHOT)
    creation = respx.post(BASE + "sandbox").respond(200, json={**SANDBOX, "state": "creating"})
    respx.get(BASE + "sandbox/sb-test").mock(
        side_effect=[
            httpx.Response(200, json={**SANDBOX, "state": "starting"}),
            httpx.Response(200, json=SANDBOX),
        ]
    )
    command = respx.post(COMMAND).respond(200, json={"exitCode": 0, "result": ""})
    with patch("providers.daytona.api.asyncio.sleep", new_callable=AsyncMock):
        result = await provider.create_vm(
            name="sb-test",
            image="custom-vm",
            setup_script="true",
            env={"FOO": "hello world", "SECRETS_PROXY_CA": "Y2VydA==", "GH_TOKEN": "placeholder"},
        )
    assert snapshot.called
    request = creation.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "name": "sb-test",
        "snapshot": "custom-vm",
        "target": "eu",
        "user": "root",
        "labels": {"managed-by": "drukbox"},
        "public": False,
        "autoStopInterval": 0,
        "autoPauseInterval": 0,
        "autoDeleteInterval": -1,
        "ttlMinutes": 0,
    }
    body = json.loads(command.calls[0].request.content)
    assert body["timeout"] == 120
    assert body["command"].startswith("sudo -n bash -e -c ")
    assert "/etc/environment" in body["command"]
    assert "update-ca-certificates" in body["command"]
    assert "gh auth git-credential" in body["command"]
    assert result.provider_id == "sandbox-1"
    assert result.name == "sb-test"
    assert result.ssh_username == "root"
    assert not result.ssh_host


@respx.mock
async def test_requires_tailscale_before_api_calls(provider: DaytonaProvider):
    with pytest.raises(ProviderCommandError, match="TAILSCALE_ENABLED"):
        await provider.create_vm(name="sb-test", image="snapshot")
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize(
    "snapshot",
    [
        {"sandboxClass": "container", "state": "active"},
        {"sandboxClass": "windows", "state": "active"},
        {"sandboxClass": "linux-vm", "state": "building"},
    ],
)
async def test_rejects_unusable_snapshot_before_creation(provider: DaytonaProvider, snapshot: dict):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=snapshot)
    with pytest.raises(ProviderCommandError):
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert len(respx.calls) == 1


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
async def test_rejected_create_does_not_delete_existing_vm(provider: DaytonaProvider, status: int):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    creation = respx.post(BASE + "sandbox").respond(status, text="sensitive data")
    with pytest.raises(ProviderCommandError) as error:
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert "sensitive data" not in "".join(traceback.format_exception(error.value))
    assert creation.call_count == 1
    assert len(respx.calls) == 2


@respx.mock
async def test_lost_create_response_cleans_up_by_owned_name(provider: DaytonaProvider):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    creation = respx.post(BASE + "sandbox").mock(side_effect=httpx.ReadTimeout("lost"))
    respx.get(BASE + "sandbox/sb-test").respond(200, json=SANDBOX)
    deletion = respx.delete(BASE + "sandbox/sandbox-1").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sandbox-1").respond(404)
    with pytest.raises(ProviderTransportError):
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert creation.call_count == 1
    assert deletion.called


@respx.mock
@pytest.mark.parametrize(
    "change",
    [
        {"sandboxClass": "container"},
        {"autoPauseInterval": 60},
        {"autoStopInterval": 15},
        {"autoDeleteInterval": 0},
        {"autoDestroyAt": "2026-10-05T12:00:00Z"},
    ],
)
async def test_unexpected_vm_policy_is_cleaned_up(provider: DaytonaProvider, change: dict):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    respx.post(BASE + "sandbox").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sb-test").respond(200, json={**SANDBOX, **change})
    deletion = respx.delete(BASE + "sandbox/sandbox-1").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sandbox-1").respond(404)
    with pytest.raises(ProviderTransportError, match="bootstrap failed"):
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert deletion.called


@respx.mock
@pytest.mark.parametrize(
    "command_response",
    [
        httpx.Response(200, json={"exitCode": 1, "result": "secret output"}),
        httpx.Response(200, json={"result": "secret output"}),
        httpx.Response(503, text="secret output"),
    ],
)
async def test_failed_bootstrap_cleans_up_without_exposing_output(
    provider: DaytonaProvider,
    command_response: httpx.Response,
):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    respx.post(BASE + "sandbox").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sb-test").respond(200, json=SANDBOX)
    respx.post(COMMAND).mock(return_value=command_response)
    deletion = respx.delete(BASE + "sandbox/sandbox-1").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sandbox-1").respond(404)
    with pytest.raises(ProviderTransportError) as error:
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert "secret output" not in "".join(traceback.format_exception(error.value))
    assert deletion.called


@respx.mock
async def test_cancelled_bootstrap_cleans_up(provider: DaytonaProvider):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    respx.post(BASE + "sandbox").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sb-test").respond(200, json=SANDBOX)
    deletion = respx.delete(BASE + "sandbox/sandbox-1").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sandbox-1").respond(404)
    with (
        patch.object(
            provider.api, "bootstrap", new_callable=AsyncMock, side_effect=asyncio.CancelledError()
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert deletion.called


@respx.mock
async def test_deletion_waits_for_destroyed(provider: DaytonaProvider):
    respx.get(BASE + "sandbox/sb-test").respond(200, json=SANDBOX)
    respx.delete(BASE + "sandbox/sandbox-1").respond(200, json=SANDBOX)
    polling = respx.get(BASE + "sandbox/sandbox-1").mock(
        side_effect=[
            httpx.Response(200, json={"state": "destroying"}),
            httpx.Response(200, json={"state": "destroyed"}),
        ]
    )
    with patch("providers.daytona.api.asyncio.sleep", new_callable=AsyncMock):
        await provider.delete_vm("sb-test")
    assert polling.call_count == 2


@respx.mock
async def test_foreign_sandbox_is_never_deleted(provider: DaytonaProvider):
    respx.get(BASE + "sandbox/sb-test").respond(
        200, json={**SANDBOX, "labels": {"managed-by": "someone-else"}}
    )
    with pytest.raises(ProviderCommandError, match="does not belong"):
        await provider.delete_vm("sb-test")
    assert len(respx.calls) == 1


@respx.mock
async def test_missing_sandbox_translates_not_found(provider: DaytonaProvider):
    respx.get(BASE + "sandbox/sb-test").respond(404)
    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")


@respx.mock
@pytest.mark.parametrize(
    "url", ["https://evil.test/toolbox", "http://proxy.app.daytona.io/toolbox"]
)
async def test_toolbox_cannot_send_credentials_to_another_host(provider: DaytonaProvider, url: str):
    with pytest.raises(ProviderTransportError, match="toolbox URL"):
        await provider.api.bootstrap({**SANDBOX, "toolboxProxyUrl": url}, "true")
    assert not respx.calls


@respx.mock
async def test_diagnose_checks_authentication_and_snapshot(provider: DaytonaProvider):
    listing = respx.get(BASE + "sandbox").respond(200, json={"items": []})
    respx.get(BASE + "snapshots/drukbox-vm").respond(200, json=SNAPSHOT)
    assert "Linux VM snapshot is active" in await provider.diagnose()
    assert listing.calls[0].request.url.params["limit"] == "1"
    listing.respond(401)
    with pytest.raises(ProviderAuthError):
        await provider.diagnose()


def test_provider_is_registered():
    assert "daytona" in get_provider_names()


@respx.mock
async def test_deletion_timeout_is_retryable(provider: DaytonaProvider):
    provider.settings.lifecycle_timeout_seconds = 0.01
    respx.get(BASE + "sandbox/sb-test").respond(200, json=SANDBOX)
    respx.delete(BASE + "sandbox/sandbox-1").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sandbox-1").respond(200, json={"state": "destroying"})
    with pytest.raises(ProviderTransportError, match="deletion timed out"):
        await provider.delete_vm("sb-test")


@respx.mock
async def test_start_timeout_cleans_up(provider: DaytonaProvider):
    provider.settings.lifecycle_timeout_seconds = 0.01
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    respx.post(BASE + "sandbox").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sb-test").respond(200, json={**SANDBOX, "state": "starting"})
    deletion = respx.delete(BASE + "sandbox/sandbox-1").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sandbox-1").respond(404)
    with pytest.raises(ProviderTransportError, match="bootstrap failed"):
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert deletion.called
