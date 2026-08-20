"""Provisioning tests for the gateway path.

Hosts of a gateway provider advertise the gateway's address. Hosts of
other providers keep their provider's own coordinates, with or without
a configured gateway.
"""

from collections.abc import Generator
from unittest.mock import AsyncMock

import pytest

from core.database import async_session_factory
from core.settings import Settings, get_settings
from hosts.exceptions import ProvisioningFailedError
from hosts.models import HostStatus
from hosts.service import HostService
from providers.base import VMCreateResult

MODULE_DOCKER_SBX = "providers.docker_sbx.provider.DockerSbxProvider"


@pytest.fixture
def gateway_configured_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Settings, None, None]:
    monkeypatch.setenv("TAILSCALE_ENABLED", "false")
    monkeypatch.setenv("GATEWAY_SSH_HOST", "gateway.example.com")
    monkeypatch.setenv("GATEWAY_SSH_PORT", "2222")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _sbx_create_result() -> VMCreateResult:
    return VMCreateResult(
        provider_id="sb-x",
        name="sb-x",
        ssh_port=0,
        ssh_username="root",
        ssh_host="",
        private_key="PRIVATE",
        public_key="ssh-ed25519 AAAAPUB",
    )


async def test_gateway_provider_hosts_advertise_the_gateway(
    gateway_configured_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_vm = AsyncMock(return_value=_sbx_create_result())
    monkeypatch.setattr(f"{MODULE_DOCKER_SBX}.create_vm", create_vm)
    scan = AsyncMock(return_value=b"gateway.example.com ssh-ed25519 AAAAGW\n")
    monkeypatch.setattr("hosts.service.HostService.scan_known_hosts", scan)

    async with async_session_factory() as session:
        service = HostService(session, settings=gateway_configured_settings)
        host = await service.create_host(env={}, image=None, provider="docker-sbx")

    assert host.status == HostStatus.ACTIVE.value
    assert host.external_ssh_host == "gateway.example.com"
    assert host.external_ssh_port == 2222
    assert host.ssh_username == host.name
    assert host.public_key == "ssh-ed25519 AAAAPUB"
    # The caller still gets the private key exactly once.
    assert host.private_key == "PRIVATE"


async def test_gateway_provider_hosts_fail_cleanly_without_an_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A gateway-provider host is reachable only through the gateway. Without
    # an address, provisioning must fail loudly, not hand out dead
    # coordinates.
    monkeypatch.setenv("TAILSCALE_ENABLED", "false")
    monkeypatch.delenv("GATEWAY_SSH_HOST", raising=False)
    get_settings.cache_clear()

    create_vm = AsyncMock(return_value=_sbx_create_result())
    monkeypatch.setattr(f"{MODULE_DOCKER_SBX}.create_vm", create_vm)

    async with async_session_factory() as session:
        service = HostService(session, settings=get_settings())
        with pytest.raises(ProvisioningFailedError, match="GATEWAY_SSH_HOST"):
            await service.create_host(env={}, image=None, provider="docker-sbx")

    get_settings.cache_clear()
    create_vm.assert_not_awaited()


async def test_direct_dial_provider_hosts_ignore_the_gateway(
    gateway_configured_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_vm = AsyncMock(
        return_value=VMCreateResult(
            provider_id="vm-1",
            name="vm-1",
            ssh_port=22,
            ssh_username="exedev",
            ssh_host="vm-1.public.example.com",
        )
    )
    monkeypatch.setattr("providers.exe.provider.ExeProvider.create_vm", create_vm)
    scan = AsyncMock(return_value=b"vm-1.public.example.com ssh-ed25519 AAAATEST\n")
    monkeypatch.setattr("hosts.service.HostService.scan_known_hosts", scan)

    async with async_session_factory() as session:
        service = HostService(session, settings=gateway_configured_settings)
        host = await service.create_host(env={}, image=None, provider="exe")

    assert host.external_ssh_host == "vm-1.public.example.com"
    assert host.external_ssh_port == 22
    assert host.ssh_username == "exedev"
    assert host.public_key == ""
