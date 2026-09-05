from unittest.mock import AsyncMock

import pytest
from uuid6 import uuid7

from core.database import async_session_factory
from core.settings import get_settings
from hosts.exceptions import HostStateError
from hosts.models import Host, HostStatus
from hosts.service import HostService, utc_now
from http_proxies.exceptions import (
    HTTPProxyExistsError,
    HTTPProxyNotFoundError,
    HTTPProxyUnsupportedError,
)
from http_proxies.service import HTTPProxyService
from providers.exceptions import (
    ProviderHttpProxyExistsError,
    ProviderHttpProxyNotFoundError,
    ProviderTargetVMNotFoundError,
)
from providers.exe.settings import ExeSettings


async def test_create_http_proxy_calls_exe_without_attachment(monkeypatch) -> None:
    mocked_create = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.create_http_proxy", mocked_create)

    async with async_session_factory() as session:
        service = HTTPProxyService(session)
        await service.create_http_proxy(
            name="gmail-mcp",
            target="https://gmailmcp.googleapis.com",
            headers={"Authorization": "Bearer token"},
        )

    mocked_create.assert_awaited_once_with(
        name="gmail-mcp",
        target="https://gmailmcp.googleapis.com",
        headers={"Authorization": "Bearer token"},
    )


async def test_create_http_proxy_maps_already_exists(monkeypatch) -> None:
    monkeypatch.setattr(
        "providers.exe.provider.ExeProvider.create_http_proxy",
        AsyncMock(side_effect=ProviderHttpProxyExistsError("exists")),
    )

    async with async_session_factory() as session:
        with pytest.raises(HTTPProxyExistsError):
            await HTTPProxyService(session).create_http_proxy(
                name="gmail-mcp",
                target="https://gmailmcp.googleapis.com",
                headers={"Authorization": "Bearer token"},
            )


async def test_create_http_proxy_rejects_non_exe_default(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "default_host_provider", "docker")

    async with async_session_factory() as session:
        with pytest.raises(HTTPProxyUnsupportedError, match="docker"):
            await HTTPProxyService(session).create_http_proxy(
                name="gmail-mcp",
                target="https://gmailmcp.googleapis.com",
                headers={"Authorization": "Bearer token"},
            )


async def test_delete_http_proxy_maps_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        "providers.exe.provider.ExeProvider.delete_http_proxy",
        AsyncMock(side_effect=ProviderHttpProxyNotFoundError("missing proxy")),
    )

    async with async_session_factory() as session:
        with pytest.raises(HTTPProxyNotFoundError):
            await HTTPProxyService(session).delete_http_proxy("gmail-mcp")


async def test_attach_and_detach_use_host_vm_name(monkeypatch) -> None:
    mocked_attach = AsyncMock()
    mocked_detach = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.attach_http_proxy", mocked_attach)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.detach_http_proxy", mocked_detach)
    host = await _create_host_record(name="sb-test", status=HostStatus.ACTIVE.value)

    async with async_session_factory() as session:
        service = HTTPProxyService(session)
        await service.attach_http_proxy("gmail-mcp", host.id)
        await service.detach_http_proxy("gmail-mcp", host.id)

    mocked_attach.assert_awaited_once_with("gmail-mcp", attach_vm="sb-test")
    mocked_detach.assert_awaited_once_with("gmail-mcp", attach_vm="sb-test")


async def test_attach_uses_non_default_exe_provider(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "default_host_provider", "docker")
    mocked_attach = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.attach_http_proxy", mocked_attach)
    host = await _create_host_record(name="sb-test", status=HostStatus.ACTIVE.value)

    async with async_session_factory() as session:
        await HTTPProxyService(session).attach_http_proxy("gmail-mcp", host.id)

    mocked_attach.assert_awaited_once_with("gmail-mcp", attach_vm="sb-test")


async def test_attach_rejects_non_exe_host(monkeypatch) -> None:
    mocked_attach = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.attach_http_proxy", mocked_attach)
    host = await _create_host_record(
        name="sb-test",
        status=HostStatus.ACTIVE.value,
        provider="docker",
    )

    async with async_session_factory() as session:
        with pytest.raises(HTTPProxyUnsupportedError, match="docker"):
            await HTTPProxyService(session).attach_http_proxy("gmail-mcp", host.id)

    mocked_attach.assert_not_awaited()


async def test_attach_rejects_host_without_backing_vm(monkeypatch) -> None:
    mocked_attach = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.attach_http_proxy", mocked_attach)
    host = await _create_host_record(name="sb-test", status=HostStatus.CREATING_VM.value)

    async with async_session_factory() as session:
        with pytest.raises(HostStateError, match="host does not have a backing VM"):
            await HTTPProxyService(session).attach_http_proxy("gmail-mcp", host.id)

    mocked_attach.assert_not_awaited()


async def test_attach_maps_missing_vm(monkeypatch) -> None:
    monkeypatch.setattr(
        "providers.exe.provider.ExeProvider.attach_http_proxy",
        AsyncMock(side_effect=ProviderTargetVMNotFoundError("missing vm")),
    )
    host = await _create_host_record(name="sb-test", status=HostStatus.ACTIVE.value)

    async with async_session_factory() as session:
        with pytest.raises(HostStateError, match="host does not have a backing VM"):
            await HTTPProxyService(session).attach_http_proxy("gmail-mcp", host.id)


async def test_detach_maps_missing_proxy(monkeypatch) -> None:
    monkeypatch.setattr(
        "providers.exe.provider.ExeProvider.detach_http_proxy",
        AsyncMock(side_effect=ProviderHttpProxyNotFoundError("missing proxy")),
    )
    host = await _create_host_record(name="sb-test", status=HostStatus.ACTIVE.value)

    async with async_session_factory() as session:
        with pytest.raises(HTTPProxyNotFoundError):
            await HTTPProxyService(session).detach_http_proxy("gmail-mcp", host.id)


async def test_detach_maps_missing_vm(monkeypatch) -> None:
    monkeypatch.setattr(
        "providers.exe.provider.ExeProvider.detach_http_proxy",
        AsyncMock(side_effect=ProviderTargetVMNotFoundError("missing vm")),
    )
    host = await _create_host_record(name="sb-test", status=HostStatus.ACTIVE.value)

    async with async_session_factory() as session:
        with pytest.raises(HostStateError, match="host does not have a backing VM"):
            await HTTPProxyService(session).detach_http_proxy("gmail-mcp", host.id)


async def test_delete_host_does_not_delete_account_proxy(monkeypatch) -> None:
    mocked_delete_proxy = AsyncMock()
    mocked_release_device = AsyncMock()
    mocked_delete_vm = AsyncMock()
    monkeypatch.setattr(
        "providers.exe.provider.ExeProvider.delete_http_proxy",
        mocked_delete_proxy,
    )
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", mocked_release_device)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", mocked_delete_vm)
    host = await _create_host_record(
        name="sb-test",
        status=HostStatus.ACTIVE.value,
        tailscale_device_id="n123CNTRL",
    )

    async with async_session_factory() as session:
        await HostService(session).delete_host(host.id)

    mocked_delete_proxy.assert_not_awaited()
    mocked_release_device.assert_awaited_once_with("n123CNTRL")
    mocked_delete_vm.assert_awaited_once_with("sb-test")


async def _create_host_record(
    *,
    name: str,
    status: str,
    provider: str = "exe",
    tailscale_device_id: str | None = None,
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
        tailscale_device_id=tailscale_device_id,
        created_at=now,
        updated_at=now,
        activated_at=now if status == HostStatus.ACTIVE.value else None,
        last_error="",
    )

    async with async_session_factory() as session:
        session.add(host)
        await session.commit()
    return host
