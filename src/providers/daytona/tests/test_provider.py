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
    "toolboxProxyUrl": TOOLBOX,
}


@pytest.fixture
async def provider() -> AsyncIterator[DaytonaProvider]:
    settings = DaytonaSettings(api_key="test-key", default_image="drukbox-vm", target="eu")
    provider = DaytonaProvider(DaytonaAPI(settings), settings, service_label="drukbox")
    yield provider
    await provider.aclose()


@respx.mock
@pytest.mark.parametrize("sandbox_class", ["linux-vm", "container"])
async def test_create_uses_linux_snapshot_and_bootstraps_tailnet(
    provider: DaytonaProvider, sandbox_class: str
):
    respx.get(BASE + "snapshots/custom-snapshot").respond(
        200, json={**SNAPSHOT, "sandboxClass": sandbox_class}
    )
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
            image="custom-snapshot",
            setup_script="true",
            env={"FOO": "hello world"},
        )
    request = creation.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "name": "sb-test",
        "snapshot": "custom-snapshot",
        "target": "eu",
        "user": "root",
        "labels": {"managed-by": "drukbox"},
        "public": False,
        "autoStopInterval": 0,
        "autoPauseInterval": 0,
        "autoDeleteInterval": -1,
        "ttlMinutes": 0,
    }
    script = json.loads(command.calls[0].request.content)["command"]
    assert script.startswith("sudo -n bash -e -c ")
    assert "/etc/environment" in script
    assert result.provider_id == "sandbox-1"
    assert result.name == "sb-test"
    assert result.ssh_username == "root"
    assert not result.ssh_host


@respx.mock
@pytest.mark.parametrize(
    "snapshot",
    [
        {"sandboxClass": "windows", "state": "active"},
        {"sandboxClass": "container", "state": "building"},
    ],
)
async def test_rejects_unusable_snapshot_before_creation(provider: DaytonaProvider, snapshot: dict):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=snapshot)
    with pytest.raises(ProviderCommandError):
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert len(respx.calls) == 1


@respx.mock
async def test_rejected_create_names_daytona_reason(provider: DaytonaProvider):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    respx.post(BASE + "sandbox").respond(
        400, json={"message": "Snapshot is not available in region eu"}
    )
    with pytest.raises(ProviderCommandError, match="not available in region eu"):
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")


@respx.mock
@pytest.mark.parametrize(
    "command_response",
    [
        httpx.Response(200, json={"exitCode": 1, "result": "secret output"}),
        httpx.Response(400, json={"message": "secret output"}),
    ],
)
async def test_failed_bootstrap_hides_output(
    provider: DaytonaProvider, command_response: httpx.Response
):
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    respx.post(BASE + "sandbox").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sb-test").respond(200, json=SANDBOX)
    respx.post(COMMAND).mock(return_value=command_response)
    with pytest.raises(ProviderCommandError) as error:
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")
    assert "secret output" not in "".join(traceback.format_exception(error.value))


@respx.mock
async def test_start_timeout_fails_provisioning(provider: DaytonaProvider):
    provider.settings.lifecycle_timeout_seconds = 0.01
    respx.get(BASE + "snapshots/snapshot").respond(200, json=SNAPSHOT)
    respx.post(BASE + "sandbox").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sb-test").respond(200, json={**SANDBOX, "state": "starting"})
    with pytest.raises(ProviderTransportError, match="start timed out"):
        await provider.create_vm(name="sb-test", image="snapshot", setup_script="true")


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
async def test_deletion_timeout_is_retryable(provider: DaytonaProvider):
    provider.settings.lifecycle_timeout_seconds = 0.01
    respx.get(BASE + "sandbox/sb-test").respond(200, json=SANDBOX)
    respx.delete(BASE + "sandbox/sandbox-1").respond(200, json=SANDBOX)
    respx.get(BASE + "sandbox/sandbox-1").respond(200, json={"state": "destroying"})
    with pytest.raises(ProviderTransportError, match="deletion timed out"):
        await provider.delete_vm("sb-test")


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
async def test_diagnose_checks_authentication_and_snapshot(provider: DaytonaProvider):
    snapshot = respx.get(BASE + "snapshots/drukbox-vm").respond(200, json=SNAPSHOT)
    assert "snapshot is active" in await provider.diagnose()
    snapshot.respond(401)
    with pytest.raises(ProviderAuthError):
        await provider.diagnose()


def test_provider_is_registered():
    assert "daytona" in get_provider_names()
