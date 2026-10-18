import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from networking.tailscale import (
    DeviceDiscoveryTimeoutError,
    NetworkAuthError,
    NetworkTransportError,
    Tailscale,
    TailscaleAPI,
    TailscaleAuthKey,
    TailscaleDevice,
)

# --- TailscaleAPI (raw HTTP) -------------------------------------------------


def _api() -> TailscaleAPI:
    return TailscaleAPI(
        oauth_client_id="client-id",
        oauth_client_secret="secret",
        tailnet="example.ts.net",
        auth_key_tags=("tag:sandbox",),
        timeout=30,
    )


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_create_auth_key_posts_tailscale_key_payload(respx_mock):
    respx_mock.post("/api/v2/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600}),
    )
    keys_route = respx_mock.post("/api/v2/tailnet/example.ts.net/keys").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "key-id",
                "key": "tskey-secret",
                "description": "auto host",
                "keyType": "auth",
            },
        ),
    )

    api = _api()
    auth_key = await api.create_auth_key(description="auto host")

    assert auth_key.key == "tskey-secret"
    assert keys_route.called
    request_body = keys_route.calls.last.request.read()
    payload = json.loads(request_body)
    assert payload["capabilities"]["devices"]["create"]["tags"] == ["tag:sandbox"]
    assert payload["description"] == "auto host"
    assert keys_route.calls.last.request.headers["authorization"] == "Bearer access-token"


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_get_access_token_reuses_cached_token(respx_mock):
    token_route = respx_mock.post("/api/v2/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600}),
    )

    api = _api()
    assert await api.get_access_token() == "access-token"
    assert await api.get_access_token() == "access-token"

    assert token_route.call_count == 1


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_delete_device_uses_device_endpoint(respx_mock):
    respx_mock.post("/api/v2/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600}),
    )
    delete_route = respx_mock.delete("/api/v2/device/n123CNTRL").mock(
        return_value=httpx.Response(200),
    )

    api = _api()
    await api.delete_device("n123CNTRL")

    assert delete_route.called
    assert delete_route.calls.last.request.headers["authorization"] == "Bearer access-token"


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_delete_device_accepts_empty_success_response(respx_mock):
    respx_mock.post("/api/v2/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600}),
    )
    delete_route = respx_mock.delete("/api/v2/device/n123CNTRL").mock(
        return_value=httpx.Response(204),
    )

    api = _api()
    await api.delete_device("n123CNTRL")

    assert delete_route.called


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_delete_device_treats_404_as_success(respx_mock):
    # Ephemeral devices auto-delete, retries are legitimate, and operator
    # cleanup via the admin console can race us. delete_device must treat
    # not-found as "this device is gone, the caller's intent is satisfied"
    # — otherwise any host whose Tailscale device was reaped out-of-band
    # can never be cleanly torn down via the drukbox API.
    respx_mock.post("/api/v2/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600}),
    )
    delete_route = respx_mock.delete("/api/v2/device/n-already-gone").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"}),
    )

    api = _api()
    await api.delete_device("n-already-gone")  # must not raise.

    assert delete_route.called


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_request_maps_tailscale_auth_error(respx_mock):
    respx_mock.post("/path").mock(
        return_value=httpx.Response(401, json={"message": "bad auth"}),
    )

    api = _api()
    with pytest.raises(NetworkAuthError):
        await api._request("POST", "/path", headers={})


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_request_maps_json_error_without_message(respx_mock):
    # The OAuth token endpoint returns {"error": ...} instead of {"message": ...};
    # a missing "message" must still surface as NetworkTransportError, not KeyError.
    respx_mock.post("/path").mock(
        return_value=httpx.Response(400, json={"error": "invalid_client"}),
    )

    api = _api()
    with pytest.raises(NetworkTransportError, match="invalid_client"):
        await api._request("POST", "/path", headers={})


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_request_rejects_non_json_response(respx_mock):
    respx_mock.post("/path").mock(
        return_value=httpx.Response(500, content=b"not json"),
    )

    api = _api()
    with pytest.raises(NetworkTransportError, match="non-JSON"):
        await api._request("POST", "/path", headers={})


# --- Tailscale (high level) --------------------------------------------------


def _make_tailscale(api: object) -> Tailscale:
    return Tailscale(api)  # type: ignore[arg-type]


def _fast_tailscale(api: object) -> Tailscale:
    # The shared `_DeviceWaiter` polls on the cadence configured at
    # construction time; tests that exercise the polling loop need a tight
    # interval so they don't sit on the default 2s sleep.
    return Tailscale(api, poll_interval=0.01)  # type: ignore[arg-type]


async def test_issue_join_credentials_returns_authkey_env() -> None:
    api = SimpleNamespace(
        tailnet="example.ts.net",
        create_auth_key=AsyncMock(
            return_value=TailscaleAuthKey(
                id="key-id",
                key="tskey-secret",
                description="auto sb-1234",
                key_type="auth",
            ),
        ),
    )
    network = _make_tailscale(api)

    creds = await network.issue_join_credentials(host_name="sb-1234")

    api.create_auth_key.assert_awaited_once_with(description="drukbox sb-1234", ephemeral=True)
    assert creds.env == {
        "TAILSCALE_AUTHKEY": "tskey-secret",
        "TAILSCALE_HOSTNAME": "sb-1234",
    }
    assert creds.device_ref is None


async def test_release_device_delegates_to_api() -> None:
    api = SimpleNamespace(
        tailnet="example.ts.net",
        delete_device=AsyncMock(),
    )
    network = _make_tailscale(api)

    await network.release_device("n123CNTRL")
    api.delete_device.assert_awaited_once_with("n123CNTRL")


def test_build_ssh_host_appends_tailnet() -> None:
    api = SimpleNamespace(tailnet="example.ts.net")
    network = _make_tailscale(api)

    assert network.build_ssh_host("sb-1234") == "sb-1234.example.ts.net"


async def test_aclose_delegates_to_api() -> None:
    api = SimpleNamespace(tailnet="example.ts.net", aclose=AsyncMock())
    network = _make_tailscale(api)

    await network.aclose()
    api.aclose.assert_awaited_once_with()


def test_from_settings_constructs_with_tailscale_api() -> None:
    network = Tailscale.from_settings()
    assert network
    assert isinstance(network.api, TailscaleAPI)


# --- list_devices ------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock(base_url="https://api.tailscale.com")
async def test_list_devices_returns_hostname_and_node_id(respx_mock):
    respx_mock.post("/api/v2/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600}),
    )
    respx_mock.get("/api/v2/tailnet/example.ts.net/devices").mock(
        return_value=httpx.Response(
            200,
            json={
                "devices": [
                    {"nodeId": "n111AAA", "hostname": "sb-aaa"},
                    {"nodeId": "n222BBB", "hostname": "sb-bbb"},
                ],
            },
        ),
    )

    api = _api()
    devices = await api.list_devices()

    assert devices == [
        TailscaleDevice(id="n111AAA", hostname="sb-aaa"),
        TailscaleDevice(id="n222BBB", hostname="sb-bbb"),
    ]


# --- Tailscale.wait_for_device -----------------------------------------------


async def test_wait_for_device_returns_device_id_when_hostname_matches() -> None:
    api = SimpleNamespace(
        tailnet="example.ts.net",
        aclose=AsyncMock(),
        list_devices=AsyncMock(
            return_value=[
                TailscaleDevice(id="n111", hostname="sb-other"),
                TailscaleDevice(id="n222", hostname="sb-target"),
            ],
        ),
    )
    network = _fast_tailscale(api)

    device_id = await network.wait_for_device(host_name="sb-target", timeout=1.0)
    await network.aclose()

    assert device_id == "n222"
    api.list_devices.assert_awaited_once()


async def test_wait_for_device_polls_until_device_appears() -> None:
    api = SimpleNamespace(
        tailnet="example.ts.net",
        aclose=AsyncMock(),
        list_devices=AsyncMock(
            side_effect=[
                [],
                [TailscaleDevice(id="n222", hostname="sb-other")],
                [TailscaleDevice(id="n333", hostname="sb-target")],
            ],
        ),
    )
    network = _fast_tailscale(api)

    device_id = await network.wait_for_device(host_name="sb-target", timeout=1.0)
    await network.aclose()

    assert device_id == "n333"
    assert api.list_devices.await_count == 3


async def test_wait_for_device_raises_after_timeout() -> None:
    api = SimpleNamespace(
        tailnet="example.ts.net",
        aclose=AsyncMock(),
        list_devices=AsyncMock(return_value=[]),
    )
    network = _fast_tailscale(api)

    with pytest.raises(DeviceDiscoveryTimeoutError):
        await network.wait_for_device(host_name="sb-never", timeout=0.05)
    await network.aclose()


async def test_wait_for_device_swallows_transient_network_errors() -> None:
    # A flaky Tailscale API shouldn't abort a provisioning task: the next poll
    # gets another shot before the timeout fires.
    api = SimpleNamespace(
        tailnet="example.ts.net",
        aclose=AsyncMock(),
        list_devices=AsyncMock(
            side_effect=[
                NetworkTransportError("boom"),
                [TailscaleDevice(id="n777", hostname="sb-recovered")],
            ],
        ),
    )
    network = _fast_tailscale(api)

    device_id = await network.wait_for_device(host_name="sb-recovered", timeout=1.0)
    await network.aclose()

    assert device_id == "n777"
    assert api.list_devices.await_count == 2


async def test_wait_for_device_fails_fast_on_auth_error() -> None:
    # A bad OAuth token won't heal on retry, so the waiter must surface the auth
    # error immediately instead of sitting until the discovery timeout and
    # misreporting broken credentials as a missing device.
    api = SimpleNamespace(
        tailnet="example.ts.net",
        aclose=AsyncMock(),
        list_devices=AsyncMock(side_effect=NetworkAuthError("bad token")),
    )
    network = _fast_tailscale(api)

    with pytest.raises(NetworkAuthError, match="bad token"):
        await network.wait_for_device(host_name="sb-a", timeout=1.0)
    await network.aclose()


async def test_wait_for_device_shares_listing_across_concurrent_waiters() -> None:
    # The whole point of `_DeviceWaiter`: four provisions waiting in parallel
    # should be served by a SINGLE `list_devices()` call per poll cycle, not
    # one per waiter. If this regresses we lose the burst-load reduction that
    # motivated the refactor.
    api = SimpleNamespace(
        tailnet="example.ts.net",
        aclose=AsyncMock(),
        list_devices=AsyncMock(
            return_value=[
                TailscaleDevice(id="n1", hostname="sb-a"),
                TailscaleDevice(id="n2", hostname="sb-b"),
                TailscaleDevice(id="n3", hostname="sb-c"),
                TailscaleDevice(id="n4", hostname="sb-d"),
            ],
        ),
    )
    network = _fast_tailscale(api)

    results = await asyncio.gather(
        network.wait_for_device(host_name="sb-a", timeout=1.0),
        network.wait_for_device(host_name="sb-b", timeout=1.0),
        network.wait_for_device(host_name="sb-c", timeout=1.0),
        network.wait_for_device(host_name="sb-d", timeout=1.0),
    )
    await network.aclose()

    assert results == ["n1", "n2", "n3", "n4"]
    # All four waiters were served by one listing — the shared poller's whole
    # purpose.
    api.list_devices.assert_awaited_once()
