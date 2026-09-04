from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from core.database import async_session_factory
from core.settings import get_settings
from hosts.exceptions import ProvisioningFailedError
from hosts.models import Host, HostStatus
from hosts.service import HostService
from providers.base import VMCreateResult
from secret_proxy.exceptions import ReverseTunnelError
from secret_proxy.tunnels import ReverseTunnelManager


async def test_bare_host_opens_and_closes_its_reverse_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_vm = AsyncMock(
        return_value=VMCreateResult(
            provider_id="container-1",
            name="sb-tunnel-host",
            ssh_host="127.0.0.1",
            ssh_port=49160,
            ssh_username="root",
            private_key="caller-private-key",
        )
    )
    delete_vm = AsyncMock()
    scan_known_hosts = AsyncMock(return_value=b"host ssh-ed25519 AAAATEST\n")
    monkeypatch.setattr("providers.docker.provider.DockerProvider.create_vm", create_vm)
    monkeypatch.setattr("providers.docker.provider.DockerProvider.delete_vm", delete_vm)
    monkeypatch.setattr("hosts.service.HostService.scan_known_hosts", scan_known_hosts)
    reverse_tunnels = MagicMock(spec=ReverseTunnelManager)
    reverse_tunnels.public_key = "ssh-ed25519 AAAATUNNEL"
    reverse_tunnels.ensure = AsyncMock()
    reverse_tunnels.close = AsyncMock()

    async with async_session_factory() as session:
        service = HostService(session, reverse_tunnels=reverse_tunnels)
        host = await service.create_host(env={}, image=None, provider="docker")
        await service.delete_host(host.id)

    assert host.status == HostStatus.ACTIVE.value
    assert create_vm.await_args
    assert create_vm.await_args.kwargs["authorized_keys"] == ("ssh-ed25519 AAAATUNNEL",)
    reverse_tunnels.ensure.assert_awaited_once_with(host, timeout_seconds=30.0)
    reverse_tunnels.close.assert_awaited_once_with(host.id)
    delete_vm.assert_awaited_once_with("sb-tunnel-host")


async def test_pool_claim_confirms_its_reverse_tunnel() -> None:
    now = datetime.now(UTC)
    pool_host = Host(
        name="sb-pool-tunnel",
        status=HostStatus.ACTIVE.value,
        provider="docker",
        image="sandbox:latest",
        external_ssh_host="127.0.0.1",
        external_ssh_port=49162,
        ssh_username="root",
        known_hosts="host ssh-ed25519 AAAATEST\n",
        secrets={},
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        pool_member=True,
    )
    reverse_tunnels = MagicMock(spec=ReverseTunnelManager)
    reverse_tunnels.ensure = AsyncMock()
    settings = get_settings().model_copy(update={"pool_sizes": {"docker": 1}})

    async with async_session_factory() as session:
        session.add(pool_host)
        await session.commit()
        claimed = await HostService(
            session,
            settings=settings,
            reverse_tunnels=reverse_tunnels,
        ).get_or_create_host(env={}, image=None, provider="docker")

    assert claimed.id == pool_host.id
    assert claimed.claimed_at
    reverse_tunnels.ensure.assert_awaited_once_with(claimed, timeout_seconds=30.0)


async def test_tunnel_start_failure_leaves_a_retryable_host_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_vm = AsyncMock(
        return_value=VMCreateResult(
            provider_id="container-1",
            name="sb-tunnel-failure",
            ssh_host="127.0.0.1",
            ssh_port=49161,
            ssh_username="root",
        )
    )
    monkeypatch.setattr("providers.docker.provider.DockerProvider.create_vm", create_vm)
    monkeypatch.setattr(
        "hosts.service.HostService.scan_known_hosts",
        AsyncMock(return_value=b"host ssh-ed25519 AAAATEST\n"),
    )
    reverse_tunnels = MagicMock(spec=ReverseTunnelManager)
    reverse_tunnels.public_key = "ssh-ed25519 AAAATUNNEL"
    reverse_tunnels.ensure = AsyncMock(
        side_effect=ReverseTunnelError("reverse tunnel could not connect")
    )

    async with async_session_factory() as session:
        service = HostService(session, reverse_tunnels=reverse_tunnels)
        with pytest.raises(ProvisioningFailedError, match="reverse tunnel could not connect"):
            await service.create_host(env={}, image=None, provider="docker")

        host = (
            await session.execute(select(Host).where(Host.name == "sb-tunnel-failure"))
        ).scalar_one()

    assert host.status == HostStatus.ERROR.value
    assert host.last_error == "ReverseTunnelError: reverse tunnel could not connect"
    assert host.expires_at


async def test_tunnel_key_failure_leaves_a_retryable_host_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_vm = AsyncMock()
    monkeypatch.setattr("providers.docker.provider.DockerProvider.create_vm", create_vm)
    monkeypatch.setattr(
        "hosts.service.load_reverse_tunnel_key",
        MagicMock(side_effect=ReverseTunnelError("reverse tunnel key could not be loaded")),
    )

    async with async_session_factory() as session:
        service = HostService(session)
        with pytest.raises(
            ProvisioningFailedError,
            match="reverse tunnel key could not be loaded",
        ):
            await service.create_host(env={}, image=None, provider="docker")

        host = (
            await session.execute(select(Host).where(Host.status == HostStatus.ERROR.value))
        ).scalar_one()

    create_vm.assert_not_awaited()
    assert host.last_error == "ReverseTunnelError: reverse tunnel key could not be loaded"
    assert host.expires_at
