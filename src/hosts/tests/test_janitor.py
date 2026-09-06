from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from uuid6 import uuid7

from core.database import async_session_factory
from hosts.janitor import reap_expired_hosts
from hosts.models import Host, HostStatus
from hosts.service import HostService, utc_now
from hosts.tests.conftest import StubVMProvider
from providers.exe.settings import ExeSettings


async def _create_host(
    *,
    name: str,
    status: str,
    expires_at: datetime | None,
    tailscale_device_id: str | None = None,
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
        tailscale_device_id=tailscale_device_id,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        last_error="",
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()
        await session.refresh(host)
    return host


async def test_janitor_deletes_host_past_expires_at(monkeypatch):
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", AsyncMock())
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", AsyncMock())
    host = await _create_host(
        name="lb-sandbox-expired",
        status=HostStatus.ACTIVE.value,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        tailscale_device_id="n123CNTRL",
    )

    reaped = await reap_expired_hosts()

    assert reaped == [host.id]

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is None


async def test_janitor_removes_the_secrets_of_an_expired_host_before_its_vm(
    stub_provider: StubVMProvider,
) -> None:
    injection = MagicMock(holds_value=True)
    injection.delete_secrets = AsyncMock()
    stub_provider.secret_injection = injection
    host = await _create_host(
        name="sb-expired",
        status=HostStatus.ACTIVE.value,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        provider="stub",
    )

    assert await reap_expired_hosts() == [host.id]

    injection.delete_secrets.assert_awaited_once_with(vm="sb-expired")
    assert stub_provider.deleted == ["sb-expired"]


async def test_janitor_leaves_unexpired_hosts_alone(monkeypatch):
    mocked_delete_device = AsyncMock()
    mocked_delete_vm = AsyncMock()
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", mocked_delete_device)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", mocked_delete_vm)
    host = await _create_host(
        name="lb-sandbox-fresh",
        status=HostStatus.ACTIVE.value,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tailscale_device_id="n123CNTRL",
    )

    reaped = await reap_expired_hosts()

    assert reaped == []
    mocked_delete_device.assert_not_awaited()
    mocked_delete_vm.assert_not_awaited()

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is not None


async def test_janitor_ignores_hosts_without_expires_at(monkeypatch):
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", AsyncMock())
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", AsyncMock())
    host = await _create_host(
        name="lb-sandbox-no-ttl",
        status=HostStatus.ACTIVE.value,
        expires_at=None,
        tailscale_device_id="n123CNTRL",
    )

    reaped = await reap_expired_hosts()

    assert reaped == []

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is not None


async def test_janitor_spares_a_renewed_host(monkeypatch):
    # The keepalive contract: a lapsed-but-not-yet-reaped host whose owner
    # renews in time survives the next janitor pass.
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", AsyncMock())
    delete_vm = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", delete_vm)
    host = await _create_host(
        name="lb-sandbox-renewed",
        status=HostStatus.ACTIVE.value,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    async with async_session_factory() as session:
        await HostService(session).renew_host(host.id)

    reaped = await reap_expired_hosts()

    assert reaped == []
    delete_vm.assert_not_awaited()
    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is not None


async def test_janitor_delete_skips_a_host_renewed_in_the_race(monkeypatch):
    # A host renewed between the janitor's expired-id selection and the locked
    # per-row delete must be spared — a 200 renewal is a keepalive promise.
    delete_vm = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", delete_vm)
    host = await _create_host(
        name="lb-sandbox-raced",
        status=HostStatus.ACTIVE.value,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    # The janitor already selected this id as expired; the owner's renewal
    # commits before the janitor's delete acquires the row lock.
    async with async_session_factory() as session:
        await HostService(session).renew_host(host.id)

    async with async_session_factory() as session:
        deleted = await HostService(session).delete_host(host.id, force=True, expired_only=True)

    assert not deleted
    delete_vm.assert_not_awaited()
    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is not None


async def test_reap_does_not_report_a_host_renewed_in_the_race(monkeypatch):
    # End-to-end reporting under the renewal race: the janitor selected the id
    # as expired, the owner's renewal lands before the locked delete — the
    # host survives and must not be logged or returned as reaped.
    delete_vm = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", delete_vm)

    real_delete_host = HostService.delete_host

    async def renew_then_delete(self, host_id, **kwargs):
        async with async_session_factory() as session:
            await HostService(session).renew_host(host_id)
        return await real_delete_host(self, host_id, **kwargs)

    monkeypatch.setattr("hosts.service.HostService.delete_host", renew_then_delete)
    host = await _create_host(
        name="lb-sandbox-race-report",
        status=HostStatus.ACTIVE.value,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    reaped = await reap_expired_hosts()

    assert reaped == []
    delete_vm.assert_not_awaited()
    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is not None


async def test_janitor_force_reaps_abandoned_early_state_host(monkeypatch):
    # An expired safety TTL on an early-state row means provisioning was
    # abandoned (a live provision's TTL is still in the future). The janitor
    # force-reaps it and tears down any VM partial provisioning may have created.
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", AsyncMock())
    delete_vm = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", delete_vm)
    host = await _create_host(
        name="lb-sandbox-stuck",
        status=HostStatus.CREATING_VM.value,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    reaped = await reap_expired_hosts()

    assert reaped == [host.id]
    delete_vm.assert_awaited_once_with("lb-sandbox-stuck")
    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is None
