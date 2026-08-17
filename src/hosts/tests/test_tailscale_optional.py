"""End-to-end-ish tests for provisioning without the tailnet.

These exercise HostService.provision() with Tailscale out of play — the
service disabled entirely, or a provider whose hosts can't join (docker):
no auth-key minting, no device discovery, no setup script delivered to
the VM, no tailnet device released on teardown. The external_ssh_host
comes from the VM provider; the keyscan runs against that address
directly.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.database import async_session_factory
from core.settings import Settings, get_settings
from hosts.models import Host, HostStatus
from hosts.service import HostService
from providers.base import VMCreateResult


@pytest.fixture
def tailscale_disabled_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Settings, None, None]:
    monkeypatch.setenv("TAILSCALE_ENABLED", "false")
    # Clear the four credential vars so this test fixture really exercises
    # the "no Tailscale credentials at all" deployment shape.
    for key in (
        "TAILSCALE_TAILNET",
        "TAILSCALE_AUTH_TAGS",
        "TAILSCALE_OAUTH_CLIENT_ID",
        "TAILSCALE_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


async def test_provision_skips_tailscale_when_disabled(
    tailscale_disabled_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mocks for the Tailscale module: we assert they were NOT called.
    issue_join = AsyncMock()
    wait_for_device = AsyncMock()
    monkeypatch.setattr("networking.tailscale.Tailscale.issue_join_credentials", issue_join)
    monkeypatch.setattr("networking.tailscale.Tailscale.wait_for_device", wait_for_device)

    # VM provider returns an external SSH address.
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

    # Keyscan returns a one-line known_hosts blob.
    scan = AsyncMock(return_value=b"vm-1.public.example.com ssh-ed25519 AAAATEST\n")
    monkeypatch.setattr("hosts.service.HostService.scan_known_hosts", scan)

    async with async_session_factory() as session:
        service = HostService(session, settings=tailscale_disabled_settings)
        host = await service.create_host(env={}, image=None)

    assert host.status == HostStatus.ACTIVE.value
    assert host.external_ssh_host == "vm-1.public.example.com"
    assert host.external_ssh_port == 22
    assert host.internal_ssh_host is None
    assert host.tailscale_device_id is None
    assert host.known_hosts == "vm-1.public.example.com ssh-ed25519 AAAATEST\n"

    # The whole Tailscale layer was bypassed.
    issue_join.assert_not_awaited()
    wait_for_device.assert_not_awaited()

    # Provider was called WITHOUT a setup script (the bootstrap script
    # hard-requires TAILSCALE_AUTHKEY and would error on a stock image).
    assert create_vm.await_args is not None
    call_kwargs = create_vm.await_args.kwargs
    assert call_kwargs["setup_script"] is None
    assert "TAILSCALE_AUTHKEY" not in (call_kwargs.get("env") or {})


async def test_docker_host_stays_local_on_a_tailnet_mode_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TAILSCALE_ENABLED=true serves remote VMs over the tailnet, but a local
    # container has no path onto it — a docker host skips the join entirely
    # and keeps its published 127.0.0.1 port as the only path.
    tailscale = AsyncMock()

    create_vm = AsyncMock(
        return_value=VMCreateResult(
            provider_id="vm-3",
            name="vm-3",
            ssh_port=32769,
            ssh_username="sandbox",
            ssh_host="127.0.0.1",
        )
    )
    monkeypatch.setattr("providers.docker.provider.DockerProvider.create_vm", create_vm)
    scan = AsyncMock(return_value=b"127.0.0.1 ssh-ed25519 AAAATEST\n")
    monkeypatch.setattr("hosts.service.HostService.scan_known_hosts", scan)

    async with async_session_factory() as session:
        service = HostService(session, tailscale=tailscale)
        host = await service.create_host(env={}, image=None, provider="docker")

    assert host.status == HostStatus.ACTIVE.value
    assert host.external_ssh_host == "127.0.0.1"
    assert host.external_ssh_port == 32769
    assert host.internal_ssh_host is None
    assert host.tailscale_device_id is None

    # The tailnet layer never came into play for this host.
    tailscale.issue_join_credentials.assert_not_awaited()
    tailscale.wait_for_device.assert_not_awaited()
    assert create_vm.await_args is not None
    call_kwargs = create_vm.await_args.kwargs
    assert call_kwargs["setup_script"] is None
    assert "TAILSCALE_AUTHKEY" not in (call_kwargs.get("env") or {})


async def test_delete_skips_tailscale_release_when_disabled(
    tailscale_disabled_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host that was somehow created without Tailscale shouldn't have a
    # device_id, so release_device is never called. This locks that in.
    release = AsyncMock()
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", release)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", AsyncMock())

    now = datetime.now(UTC)
    async with async_session_factory() as session:
        host = Host(
            name="vm-2",
            status=HostStatus.ACTIVE.value,
            provider="exe",
            image="img:latest",
            env={},
            external_ssh_host="vm-2.public.example.com",
            external_ssh_port=22,
            internal_ssh_host=None,
            tailscale_device_id=None,
            known_hosts="",
            last_error="",
            created_at=now,
            updated_at=now,
        )
        session.add(host)
        await session.commit()
        host_id = host.id

    async with async_session_factory() as session:
        service = HostService(session, settings=tailscale_disabled_settings)
        await service.delete_host(host_id)

    release.assert_not_awaited()
