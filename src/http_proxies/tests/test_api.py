import uuid
from unittest.mock import AsyncMock

from uuid6 import uuid7

from core.database import async_session_factory
from core.settings import get_settings
from hosts.models import Host, HostStatus
from hosts.service import utc_now
from providers.exe.settings import ExeSettings


async def test_create_http_proxy_returns_created(client, monkeypatch) -> None:
    mocked_create = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.create_http_proxy", mocked_create)

    response = await client.post(
        "/http-proxies",
        headers={"Authorization": "Bearer service-token"},
        json={
            "name": "gmail-mcp",
            "target": "https://gmailmcp.googleapis.com",
            "headers": {"Authorization": "Bearer token"},
        },
    )

    assert response.status_code == 201
    assert response.json() == {"name": "gmail-mcp", "status": "created"}
    mocked_create.assert_awaited_once_with(
        name="gmail-mcp",
        target="https://gmailmcp.googleapis.com/",
        headers={"Authorization": "Bearer token"},
    )


async def test_create_http_proxy_requires_service_auth(client) -> None:
    missing = await client.post("/http-proxies")
    bad = await client.post(
        "/http-proxies",
        headers={"Authorization": "Bearer wrong-token"},
        json={
            "name": "gmail-mcp",
            "target": "https://gmailmcp.googleapis.com",
            "headers": {"Authorization": "Bearer token"},
        },
    )

    assert missing.status_code == 401
    assert bad.status_code == 403


async def test_http_proxy_returns_501_for_non_exe_default(client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "default_host_provider", "docker")

    response = await client.post(
        "/http-proxies",
        headers={"Authorization": "Bearer service-token"},
        json={
            "name": "gmail-mcp",
            "target": "https://gmailmcp.googleapis.com",
            "headers": {"Authorization": "Bearer token"},
        },
    )

    assert response.status_code == 501
    assert response.json()["error_code"] == "HTTP_PROXY_UNSUPPORTED"


async def test_attach_returns_501_for_non_exe_host(client) -> None:
    host = await _create_host_record(
        name="sb-test",
        status=HostStatus.ACTIVE.value,
        provider="docker",
    )

    response = await client.post(
        f"/http-proxies/gmail-mcp/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 501
    assert response.json()["error_code"] == "HTTP_PROXY_UNSUPPORTED"


async def test_http_proxy_rejects_option_like_names(client, monkeypatch) -> None:
    blocked = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.create_http_proxy", blocked)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_http_proxy", blocked)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.attach_http_proxy", blocked)
    headers = {"Authorization": "Bearer service-token"}

    create = await client.post(
        "/http-proxies",
        headers=headers,
        json={"name": "--all", "target": "https://t.example.com", "headers": {"H": "v"}},
    )
    delete = await client.delete("/http-proxies/--all", headers=headers)
    attach = await client.post(f"/http-proxies/--all/hosts/{uuid.uuid4()}", headers=headers)

    assert create.status_code == 422
    assert delete.status_code == 422
    assert attach.status_code == 422
    blocked.assert_not_awaited()


async def test_create_http_proxy_rejects_credentialed_or_non_origin_target(client) -> None:
    headers = {"Authorization": "Bearer service-token"}
    payload = {
        "name": "gmail-mcp",
        "headers": {"Authorization": "Bearer token"},
    }

    credentialed = await client.post(
        "/http-proxies",
        headers=headers,
        json={**payload, "target": "https://user:pass@gmailmcp.googleapis.com"},
    )
    with_path = await client.post(
        "/http-proxies",
        headers=headers,
        json={**payload, "target": "https://gmailmcp.googleapis.com/mcp/v1"},
    )

    assert credentialed.status_code == 422
    assert with_path.status_code == 422


async def test_create_http_proxy_rejects_invalid_url_and_headers_shape(client) -> None:
    headers = {"Authorization": "Bearer service-token"}
    invalid_url = await client.post(
        "/http-proxies",
        headers=headers,
        json={
            "name": "gmail-mcp",
            "target": "not-a-url",
            "headers": {"Authorization": "Bearer token"},
        },
    )
    invalid_headers = await client.post(
        "/http-proxies",
        headers=headers,
        json={
            "name": "gmail-mcp",
            "target": "https://gmailmcp.googleapis.com",
            "headers": ["Authorization: Bearer token"],
        },
    )

    assert invalid_url.status_code == 422
    assert invalid_headers.status_code == 422
    assert invalid_headers.json()["detail"][0]["loc"] == ["body", "headers"]


async def test_attach_and_detach_http_proxy(client, monkeypatch) -> None:
    mocked_attach = AsyncMock()
    mocked_detach = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.attach_http_proxy", mocked_attach)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.detach_http_proxy", mocked_detach)
    host = await _create_host_record(name="sb-test", status=HostStatus.ACTIVE.value)
    headers = {"Authorization": "Bearer service-token"}

    attached = await client.post(
        f"/http-proxies/gmail-mcp/hosts/{host.id}",
        headers=headers,
    )
    detached = await client.delete(
        f"/http-proxies/gmail-mcp/hosts/{host.id}",
        headers=headers,
    )

    assert attached.status_code == 200
    assert attached.json() == {
        "name": "gmail-mcp",
        "host_id": str(host.id),
        "status": "attached",
    }
    assert detached.status_code == 204
    mocked_attach.assert_awaited_once_with("gmail-mcp", attach_vm="sb-test")
    mocked_detach.assert_awaited_once_with("gmail-mcp", attach_vm="sb-test")


async def test_delete_http_proxy_returns_no_content(client, monkeypatch) -> None:
    mocked_delete = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_http_proxy", mocked_delete)

    response = await client.delete(
        "/http-proxies/gmail-mcp",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 204
    assert response.content == b""
    mocked_delete.assert_awaited_once_with("gmail-mcp")


async def test_attach_rejects_missing_host(client) -> None:
    host_id = uuid.UUID("00000000-0000-0000-0000-000000000141")

    response = await client.post(
        f"/http-proxies/gmail-mcp/hosts/{host_id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "host not found"


async def test_attach_rejects_hosts_without_backing_vm(client) -> None:
    for index, host_status in enumerate(
        (
            HostStatus.PROVISIONING.value,
            HostStatus.CREATING_NETWORK.value,
            HostStatus.CREATING_VM.value,
            HostStatus.ERROR.value,
        ),
        start=1,
    ):
        host = await _create_host_record(name=f"sb-test-{index}", status=host_status)

        response = await client.post(
            f"/http-proxies/gmail-mcp/hosts/{host.id}",
            headers={"Authorization": "Bearer service-token"},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "host does not have a backing VM"


async def _create_host_record(
    *,
    name: str,
    status: str,
    provider: str = "exe",
) -> Host:
    now = utc_now()
    host = Host(
        id=uuid7(),
        name=name,
        status=status,
        provider=provider,
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        env={},
        internal_ssh_host=f"{name}.example.ts.net",
        external_ssh_host="",
        external_ssh_port=22,
        known_hosts="",
        tailscale_device_id=None,
        created_at=now,
        updated_at=now,
        activated_at=now if status == HostStatus.ACTIVE.value else None,
        last_error="provider error" if status == HostStatus.ERROR.value else "",
    )

    async with async_session_factory() as session:
        session.add(host)
        await session.commit()
    return host
