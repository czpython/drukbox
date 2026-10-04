import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from providers.boat.api import BoatAPI
from providers.boat.provider import BoatProvider
from providers.boat.settings import BoatSettings
from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from providers.registry import get_provider_names

BASE = "https://boat.dev/api/v1"
SANDBOX = "bx_23456789"


@pytest.fixture
async def provider() -> AsyncIterator[BoatProvider]:
    settings = BoatSettings(api_token="test-token")
    instance = BoatProvider(BoatAPI(settings), settings, service_label="test")
    yield instance
    await instance.aclose()


@pytest.fixture
def sandbox(respx_mock: respx.MockRouter) -> dict[str, respx.Route]:
    return {
        "create": respx_mock.post(f"{BASE}/sandboxes").respond(
            202, json={"sandbox": {"id": SANDBOX}}
        ),
        "name": respx_mock.patch(f"{BASE}/sandboxes/{SANDBOX}").respond(200, json={}),
        "ready": respx_mock.get(f"{BASE}/sandboxes/{SANDBOX}").respond(
            200, json={"sandbox": {"state": "ready", "archiveAfter": None}}
        ),
        "command": respx_mock.post(f"{BASE}/sandboxes/{SANDBOX}/commands").respond(
            200, json={"success": True, "timedOut": False, "exitCode": 0}
        ),
        "ssh": respx_mock.post(f"{BASE}/sandboxes/{SANDBOX}/sshkey").respond(
            200, json={"machineIp": "203.0.113.10", "sshUser": "user"}
        ),
        "delete": respx_mock.delete(f"{BASE}/sandboxes/{SANDBOX}").respond(202, json={}),
    }


@pytest.mark.respx(assert_all_called=False)
async def test_provision_uses_isolated_account_environment_and_direct_ssh(provider, sandbox):
    result = await provider.create_vm(name="host-1", image="default", env={"APP_MODE": "test"})
    assert result.name == "host-1"
    assert result.provider_id == SANDBOX
    assert (result.ssh_host, result.ssh_port, result.ssh_username) == ("203.0.113.10", 22, "user")
    assert result.private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    create = sandbox["create"].calls.last.request
    assert json.loads(create.content) == {
        "type": "default",
        "ttlSeconds": None,
        "noEnv": True,
        "snapshots": False,
    }
    assert create.headers["Authorization"] == "Bearer test-token"
    assert create.headers["Idempotency-Key"]
    assert json.loads(sandbox["name"].calls.last.request.content) == {"name": "test:host-1"}
    command = json.loads(sandbox["command"].calls.last.request.content)["command"]
    assert "sudo -n bash -e" in command
    assert "APP_MODE=test" in command
    assert "/etc/environment" in command
    assert "PRIVATE KEY" not in sandbox["ssh"].calls.last.request.content.decode()
    assert not sandbox["delete"].called


@pytest.mark.respx(assert_all_called=False)
async def test_named_snapshot_and_forwarded_ssh_endpoint(provider, sandbox):
    sandbox["ssh"].respond(
        200, json={"machineIp": None, "sshEndpoint": "203.0.113.20:22001", "sshUser": "user"}
    )
    result = await provider.create_vm(name="host-1", image="agent-base", instance_type="large")
    assert (result.ssh_host, result.ssh_port) == ("203.0.113.20", 22001)
    body = json.loads(sandbox["create"].calls.last.request.content)
    assert body["from"] == "agent-base"
    assert body["type"] == "large"


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize("state", ["error", "cancelled", "archived"])
async def test_failed_provision_is_deleted(provider, sandbox, state):
    sandbox["ready"].respond(200, json={"sandbox": {"state": state}})
    with pytest.raises(ProviderCommandError):
        await provider.create_vm(name="host-1", image="default")
    assert sandbox["delete"].called
    assert not sandbox["command"].called


@pytest.mark.respx(assert_all_called=False)
async def test_ready_wait_is_bounded(provider, sandbox):
    provider.settings.provision_timeout = 0.01
    sandbox["ready"].respond(200, json={"sandbox": {"state": "provisioning"}})
    with pytest.raises(ProviderCommandError):
        await provider.create_vm(name="host-1", image="default")
    assert sandbox["delete"].called


@pytest.mark.respx(assert_all_called=False)
async def test_platform_timeout_is_not_silently_accepted(provider, sandbox):
    sandbox["ready"].respond(
        200, json={"sandbox": {"state": "ready", "archiveAfter": "2026-10-04T18:00:00Z"}}
    )
    with pytest.raises(ProviderCommandError):
        await provider.create_vm(name="host-1", image="default")
    assert sandbox["delete"].called


@pytest.mark.respx(assert_all_called=False)
async def test_bootstrap_failure_does_not_return_a_host_or_leak_output(provider, sandbox):
    sandbox["command"].respond(
        200, json={"success": False, "exitCode": 1, "timedOut": False, "stderr": "sensitive-output"}
    )
    with pytest.raises(ProviderCommandError) as error:
        await provider.create_vm(name="host-1", image="default")
    assert "sensitive-output" not in str(error.value)
    assert sandbox["delete"].called


@pytest.mark.parametrize(
    "arguments",
    [
        {"instance_type": "invalid"},
        {"disk_gb": 20},
        {"setup_script": "tailscale up"},
        {"env": {"INVALID-NAME": "value"}},
    ],
)
async def test_invalid_request_fails_before_allocation(provider, arguments, respx_mock):
    with pytest.raises(ProviderCommandError):
        await provider.create_vm(name="host-1", image="default", **arguments)
    assert not respx_mock.calls


async def test_delete_follows_pagination_and_confirms_exact_resource(provider, respx_mock):
    listing = respx_mock.get(f"{BASE}/sandboxes").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "sandboxes": [{"name": "another", "id": "other"}],
                    "pageInfo": {"hasMore": True, "nextCursor": "next"},
                },
            ),
            httpx.Response(200, json={"sandboxes": [{"name": "test:host-1", "id": SANDBOX}]}),
        ]
    )
    deletion = respx_mock.delete(f"{BASE}/sandboxes/{SANDBOX}").respond(202, json={})
    await provider.delete_vm("host-1")
    assert listing.calls.last.request.url.params["cursor"] == "next"
    assert deletion.calls.last.request.headers["X-Ascii-Confirm-Delete"] == SANDBOX


async def test_missing_sandbox_raises_neutral_error(provider, respx_mock):
    respx_mock.get(f"{BASE}/sandboxes").respond(200, json={"sandboxes": []})
    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("host-1")


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (404, ProviderNotFoundError),
        (400, ProviderCommandError),
        (429, ProviderTransportError),
        (503, ProviderTransportError),
    ],
)
async def test_http_failures_are_typed_and_do_not_echo_response(
    provider, respx_mock, status, error
):
    respx_mock.get(f"{BASE}/sandboxes").respond(status, text="secret-value")
    with pytest.raises(error) as caught:
        await provider.diagnose()
    assert "secret-value" not in str(caught.value)


async def test_diagnose_is_read_only(provider, respx_mock):
    route = respx_mock.get(f"{BASE}/sandboxes").respond(200, json={"sandboxes": []})
    assert "succeeded" in await provider.diagnose()
    assert route.calls.last.request.url.params["limit"] == "1"


async def test_transport_failure_is_typed(provider, respx_mock):
    respx_mock.get(f"{BASE}/sandboxes").mock(side_effect=httpx.ReadTimeout("secret-value"))
    with pytest.raises(ProviderTransportError, match="transport failed"):
        await provider.diagnose()


async def test_allocation_retry_reuses_idempotency_key_and_body(provider, respx_mock):
    create = respx_mock.post(f"{BASE}/sandboxes").mock(
        side_effect=[
            httpx.ReadTimeout("response lost"),
            httpx.Response(202, json={"sandbox": {"id": SANDBOX}}),
        ]
    )
    with patch("providers.boat.api.asyncio.sleep", new_callable=AsyncMock):
        result = await provider.api.create_sandbox(
            "test:host-1", image="default", instance_type="small"
        )
    assert result == SANDBOX
    first, second = (call.request for call in create.calls)
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert first.content == second.content


@pytest.mark.parametrize("body", ["not-json", "[]"])
async def test_invalid_response_is_typed(provider, respx_mock, body):
    respx_mock.get(f"{BASE}/sandboxes").respond(200, text=body)
    with pytest.raises(ProviderTransportError):
        await provider.diagnose()


def test_provider_is_registered():
    assert "boat" in get_provider_names()


async def test_close_releases_http_client(provider):
    with patch.object(provider.api, "aclose", new_callable=AsyncMock) as close:
        await provider.aclose()
    close.assert_awaited_once()
