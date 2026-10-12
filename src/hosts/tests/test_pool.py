import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from uuid6 import uuid7

from core.database import async_session_factory
from core.settings import Settings, get_settings
from hosts.exceptions import HostCapacityError
from hosts.models import Host, HostStatus
from hosts.pool import _MAX_CONSECUTIVE_CREATE_FAILURES, maintain_pool
from hosts.service import HostService, utc_now
from providers.exe.settings import ExeSettings


@pytest.fixture
def pooled_settings(monkeypatch):
    """Force the pool to be enabled for this test, via the POOL_SIZE alias."""
    monkeypatch.setenv("POOL_SIZE", "2")
    monkeypatch.delenv("POOL_SIZES", raising=False)
    monkeypatch.setenv("POOL_HOST_MAX_AGE_HOURS", "4")
    # Raise the per-tick cap so tests can observe full deficit refills in a
    # single maintain_pool() call. Production defaults to a small cap so that
    # overlapping ticks bound the over-provision blast radius.
    monkeypatch.setenv("POOL_MAX_CREATES_PER_TICK", "100")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def multi_pool_settings(monkeypatch):
    """Warm targets for two providers ("exe" and the conftest "stub")."""
    monkeypatch.setenv("POOL_SIZES", '{"exe": 2, "stub": 1}')
    monkeypatch.delenv("POOL_SIZE", raising=False)
    monkeypatch.setenv("POOL_HOST_MAX_AGE_HOURS", "4")
    monkeypatch.setenv("POOL_MAX_CREATES_PER_TICK", "100")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


async def _pool_counts_by_provider() -> dict[str, int]:
    async with async_session_factory() as session:
        counted = await session.execute(
            select(Host.provider, func.count())
            .where(Host.pool_member.is_(True))
            .where(Host.claimed_at.is_(None))
            .group_by(Host.provider)
        )
    return {provider: count for provider, count in counted.all()}


async def _seed_pool_host(
    *,
    name: str,
    provider: str = "exe",
    status: str = HostStatus.ACTIVE.value,
    claimed_at: datetime | None = None,
    expires_at: datetime | None = None,
    pool_member: bool = True,
    env: dict[str, str] | None = None,
) -> Host:
    now = utc_now()
    host = Host(
        id=uuid7(),
        name=name,
        status=status,
        provider=provider,
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        env=env or {},
        internal_ssh_host=f"{name}.example.ts.net",
        external_ssh_host="",
        external_ssh_port=22,
        known_hosts="",
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        claimed_at=claimed_at,
        pool_member=pool_member,
        last_error="",
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()
        await session.refresh(host)
    return host


async def test_create_host_claims_pool_member_when_eligible(pooled_settings, monkeypatch):
    pool_host = await _seed_pool_host(name="lb-pool-1")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        result = await service.get_or_create_host(env={}, image=None)

    assert result.id == pool_host.id
    assert result.claimed_at is not None
    mocked_provision.assert_not_awaited()


async def test_pool_never_claims_or_sheds_demand_hosts(pooled_settings, monkeypatch):
    # A demand-provisioned host keeps claimed_at NULL just like a warm pool
    # host, but pool_member=False must keep pool claim/count/shed away from it —
    # otherwise caller A's env-bearing sandbox could be handed to caller B, or
    # deleted as pool excess.
    demand = await _seed_pool_host(
        name="lb-demand-1", pool_member=False, env={"SECRET": "caller-a"}
    )
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", AsyncMock())
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", AsyncMock())

    # A later default request must fall back to fresh, not claim the demand host.
    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        result = await service.get_or_create_host(env={}, image=None)
    assert result.id != demand.id
    assert result.claimed_at is None

    # Pool maintenance must not count or shed it; the demand host survives.
    await maintain_pool()
    async with async_session_factory() as session:
        assert await session.get(Host, demand.id) is not None


async def test_create_host_falls_back_to_fresh_when_pool_empty(pooled_settings, monkeypatch):
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        result = await service.get_or_create_host(env={}, image=None)

    assert result.claimed_at is None
    assert result.status == HostStatus.PROVISIONING.value
    mocked_provision.assert_awaited_once()


async def test_create_host_skips_pool_when_env_present(pooled_settings, monkeypatch):
    await _seed_pool_host(name="lb-pool-1")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        result = await service.get_or_create_host(env={"SANDBOX_X": "value"}, image=None)

    assert result.claimed_at is None
    mocked_provision.assert_awaited_once()


async def test_create_host_skips_pool_when_provider_has_no_target(
    pooled_settings, monkeypatch, stub_provider
):
    # POOL_SIZE targets only the default provider, so a request pinned to an
    # untargeted provider must provision fresh rather than hand back a
    # default-provider member.
    await _seed_pool_host(name="lb-pool-1")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        result = await service.get_or_create_host(env={}, image=None, provider="stub")

    assert result.claimed_at is None
    assert result.provider == "stub"
    mocked_provision.assert_awaited_once()


async def test_create_host_claims_pool_member_of_the_pinned_provider(
    multi_pool_settings, monkeypatch, stub_provider
):
    await _seed_pool_host(name="lb-pool-exe")
    stub_host = await _seed_pool_host(name="lb-pool-stub", provider="stub")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async with async_session_factory() as session:
        service = HostService(session, settings=multi_pool_settings)
        result = await service.get_or_create_host(env={}, image=None, provider="stub")

    assert result.id == stub_host.id
    assert result.claimed_at is not None
    mocked_provision.assert_not_awaited()


async def test_create_host_never_claims_another_providers_pool_member(
    multi_pool_settings, monkeypatch, stub_provider
):
    # Only an exe host is warm; a stub request has a pool target but must
    # provision fresh rather than cross providers.
    exe_host = await _seed_pool_host(name="lb-pool-exe")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async with async_session_factory() as session:
        service = HostService(session, settings=multi_pool_settings)
        result = await service.get_or_create_host(env={}, image=None, provider="stub")

    assert result.id != exe_host.id
    assert result.provider == "stub"
    assert result.claimed_at is None
    mocked_provision.assert_awaited_once()


async def test_create_host_refuses_a_provider_at_its_host_limit(monkeypatch, stub_provider):
    # The warm pool member counts toward exe's limit; stub has none.
    monkeypatch.setenv("HOST_LIMITS", '{"exe": 1}')
    settings = Settings()  # pyright: ignore[reportCallIssue]
    await _seed_pool_host(name="lb-pool-exe")
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    async with async_session_factory() as session:
        with pytest.raises(HostCapacityError):
            await HostService(session, settings=settings).create_host(
                env={}, image=None, provider="exe"
            )
    async with async_session_factory() as session:
        await HostService(session, settings=settings).create_host(
            env={}, image=None, provider="stub"
        )
        counted = await session.execute(select(Host.provider, func.count()).group_by(Host.provider))

    assert {provider: count for provider, count in counted.all()} == {"exe": 1, "stub": 1}


async def test_create_host_skips_pool_when_sized(multi_pool_settings, monkeypatch, stub_provider):
    # Pool members are warmed at the provider's default size, so a sized
    # request must provision fresh even when its provider has a warm host.
    pool_host = await _seed_pool_host(name="lb-pool-stub", provider="stub")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    for instance_type, disk_gb in (("stub-large", None), (None, 200)):
        async with async_session_factory() as session:
            service = HostService(session, settings=multi_pool_settings)
            result = await service.get_or_create_host(
                env={},
                image=None,
                provider="stub",
                instance_type=instance_type,
                disk_gb=disk_gb,
            )

        assert result.id != pool_host.id
        assert result.claimed_at is None
        assert result.instance_type == instance_type
        assert result.disk_gb == disk_gb

    assert mocked_provision.await_count == 2


async def test_create_host_skips_pool_when_image_override(pooled_settings, monkeypatch):
    await _seed_pool_host(name="lb-pool-1")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        result = await service.get_or_create_host(env={}, image="custom-image:tag")

    assert result.claimed_at is None
    assert result.image == "custom-image:tag"
    mocked_provision.assert_awaited_once()


async def test_pool_claim_overrides_pool_max_age_with_caller_expires_at(
    pooled_settings, monkeypatch
):
    pool_expires_at = datetime.now(UTC) + timedelta(hours=4)
    await _seed_pool_host(name="lb-pool-1", expires_at=pool_expires_at)
    caller_expires_at = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        claimed = await service.get_or_create_host(env={}, image=None, expires_at=caller_expires_at)

    assert claimed.expires_at is not None
    assert abs((claimed.expires_at - caller_expires_at).total_seconds()) < 1


async def test_pool_claim_stamps_default_lease_when_caller_omits_expires_at(
    pooled_settings, monkeypatch
):
    # A claim without a caller expires_at carries the default lease, replacing
    # the warm-pool max-age TTL — so a claimed host never leaks forever and
    # its owner can keep it alive through renewal.
    pool_expires_at = datetime.now(UTC) + timedelta(hours=4)
    await _seed_pool_host(name="lb-pool-1", expires_at=pool_expires_at)
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        claimed = await service.get_or_create_host(env={}, image=None)  # no expires_at

    lease_end = utc_now() + timedelta(seconds=pooled_settings.lease_default_ttl)
    assert claimed.expires_at is not None
    assert abs((claimed.expires_at - lease_end).total_seconds()) < 5


async def test_pool_claim_honors_explicit_permanent_lease(pooled_settings, monkeypatch):
    # An explicit expires_at=None is the caller's deliberate opt-in to a
    # permanent host; the claim must clear the warm-pool max-age TTL.
    await _seed_pool_host(name="lb-pool-1", expires_at=datetime.now(UTC) + timedelta(hours=4))
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        claimed = await service.get_or_create_host(env={}, image=None, expires_at=None)

    assert claimed.expires_at is None


async def test_concurrent_claims_dont_grab_the_same_pool_host(pooled_settings, monkeypatch):
    pool_host = await _seed_pool_host(name="lb-pool-only")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async def attempt_claim():
        async with async_session_factory() as session:
            service = HostService(session, settings=pooled_settings)
            return await service.get_or_create_host(env={}, image=None)

    first, second = await asyncio.gather(attempt_claim(), attempt_claim())

    claim_ids = {h.id for h in (first, second) if h.claimed_at is not None}
    fresh_ids = {h.id for h in (first, second) if h.claimed_at is None}
    assert claim_ids == {pool_host.id}
    assert len(fresh_ids) == 1
    mocked_provision.assert_awaited_once()


async def test_maintain_pool_creates_up_to_pool_size(pooled_settings, monkeypatch):
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    summary = await maintain_pool()

    assert summary.created == pooled_settings.pool_size
    assert mocked_provision.await_count == pooled_settings.pool_size


async def test_maintain_pool_tops_up_only_the_deficit(pooled_settings, monkeypatch):
    await _seed_pool_host(name="lb-pool-existing")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    summary = await maintain_pool()

    assert summary.created == pooled_settings.pool_size - 1


async def test_maintain_pool_sheds_excess_when_pool_size_shrinks(pooled_settings, monkeypatch):
    # POOL_SIZE=2; seed 5 unclaimed hosts. Maintainer should shed the 3 oldest.
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", AsyncMock())
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", AsyncMock())
    for i in range(5):
        await _seed_pool_host(name=f"lb-pool-excess-{i}")

    summary = await maintain_pool()

    assert summary.removed_excess == 3
    assert summary.created == 0
    async with async_session_factory() as session:
        result = await session.execute(select(Host).where(Host.claimed_at.is_(None)))
        remaining = list(result.scalars())
    assert len(remaining) == 2


async def test_maintain_pool_tops_up_each_provider_toward_its_target(
    multi_pool_settings, monkeypatch, stub_provider
):
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    summary = await maintain_pool()

    assert summary.created == 3
    assert await _pool_counts_by_provider() == {"exe": 2, "stub": 1}


async def test_maintain_pool_sheds_excess_per_provider(monkeypatch, stub_provider):
    # exe is over target while stub is exactly on target; only exe sheds, and
    # only down to its own target.
    monkeypatch.setenv("POOL_SIZES", '{"exe": 1, "stub": 2}')
    monkeypatch.delenv("POOL_SIZE", raising=False)
    get_settings.cache_clear()
    try:
        monkeypatch.setattr("networking.tailscale.Tailscale.release_device", AsyncMock())
        monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", AsyncMock())
        for i in range(3):
            await _seed_pool_host(name=f"lb-exe-{i}")
        for i in range(2):
            await _seed_pool_host(name=f"lb-stub-{i}", provider="stub")

        summary = await maintain_pool()

        assert summary.removed_excess == 2
        assert summary.created == 0
        assert await _pool_counts_by_provider() == {"exe": 1, "stub": 2}
    finally:
        get_settings.cache_clear()


async def test_maintain_pool_sheds_provider_dropped_from_targets(monkeypatch, stub_provider):
    # stub carries no target at all: its warm hosts shed to zero instead of
    # idling until the max-age TTL, while exe tops up toward its own target.
    monkeypatch.setenv("POOL_SIZES", '{"exe": 1}')
    monkeypatch.delenv("POOL_SIZE", raising=False)
    get_settings.cache_clear()
    try:
        monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())
        monkeypatch.setattr("networking.tailscale.Tailscale.release_device", AsyncMock())
        stub_host = await _seed_pool_host(name="lb-stub-0", provider="stub")

        summary = await maintain_pool()

        assert summary.removed_excess == 1
        assert summary.created == 1
        assert stub_provider.deleted == [stub_host.name]
        assert await _pool_counts_by_provider() == {"exe": 1}
    finally:
        get_settings.cache_clear()


async def test_maintain_pool_caps_total_creates_across_providers(monkeypatch, stub_provider):
    monkeypatch.setenv("POOL_SIZES", '{"exe": 5, "stub": 5}')
    monkeypatch.delenv("POOL_SIZE", raising=False)
    monkeypatch.setenv("POOL_MAX_CREATES_PER_TICK", "3")
    get_settings.cache_clear()
    try:
        provision = AsyncMock()
        monkeypatch.setattr("hosts.service.HostService.provision", provision)

        summary = await maintain_pool()

        assert summary.created == 3
        assert provision.await_count == 3
        # The budget round-robins across providers instead of filling one
        # first: two providers under a cap of 3 split 2/1, in an order that
        # is shuffled per tick.
        counts = await _pool_counts_by_provider()
        assert set(counts) == {"exe", "stub"}
        assert sorted(counts.values()) == [1, 2]
    finally:
        get_settings.cache_clear()


async def test_maintain_pool_reaches_other_providers_when_one_keeps_failing(
    monkeypatch, stub_provider
):
    # With a per-tick cap of 1 and exe's creates permanently failing, a fixed
    # top-up order would retry exe every tick and never warm stub. The
    # per-tick shuffle must reach stub across ticks (the odds of 50 shuffles
    # all leading with exe are 2**-50).
    monkeypatch.setenv("POOL_SIZES", '{"exe": 2, "stub": 1}')
    monkeypatch.delenv("POOL_SIZE", raising=False)
    monkeypatch.setenv("POOL_MAX_CREATES_PER_TICK", "1")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())
        real_create_host = HostService.create_host

        async def create_host_with_exe_down(self, **kwargs):
            if kwargs.get("provider") == "exe":
                raise RuntimeError("exe provider down")
            return await real_create_host(self, **kwargs)

        monkeypatch.setattr("hosts.service.HostService.create_host", create_host_with_exe_down)

        for _ in range(50):
            await maintain_pool()
            if await _pool_counts_by_provider() == {"stub": 1}:
                break
        assert await _pool_counts_by_provider() == {"stub": 1}
    finally:
        get_settings.cache_clear()


async def test_maintain_pool_excludes_expired_hosts_from_count(pooled_settings, monkeypatch):
    # An unclaimed host past its expires_at is queued for janitor reaping and
    # must not count toward the pool target. Maintainer should top up to N.
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())
    await _seed_pool_host(
        name="lb-pool-expiring",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    summary = await maintain_pool()

    # The expiring host doesn't count, so the maintainer creates pool_size more.
    assert summary.created == pooled_settings.pool_size


async def test_maintain_pool_caps_creates_per_tick(monkeypatch):
    # Configure a pool whose deficit exceeds the per-tick cap. Maintainer
    # should only create up to POOL_MAX_CREATES_PER_TICK; the next tick will
    # fill the rest. This is what bounds over-provision when ticks overlap.
    monkeypatch.setenv("POOL_SIZE", "10")
    monkeypatch.setenv("POOL_HOST_MAX_AGE_HOURS", "4")
    monkeypatch.setenv("POOL_MAX_CREATES_PER_TICK", "3")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())
        summary = await maintain_pool()
        assert summary.created == 3
    finally:
        get_settings.cache_clear()


async def test_maintain_pool_breaks_after_consecutive_create_failures(pooled_settings, monkeypatch):
    # When provisioning keeps failing (e.g. provider outage), the maintainer
    # should bail out after the limit instead of looping pool_size times.
    provision = AsyncMock(side_effect=RuntimeError("provider down"))
    monkeypatch.setattr("hosts.service.HostService.provision", provision)

    # Force pool_size large enough that the cap matters.
    monkeypatch.setenv("POOL_SIZE", "10")
    get_settings.cache_clear()
    try:
        summary = await maintain_pool()
        assert summary.created == 0
        # The breaker must trip at the failure limit, not grind through all 10
        # pool slots — created==0 alone can't tell those apart.
        assert provision.await_count == _MAX_CONSECUTIVE_CREATE_FAILURES
    finally:
        get_settings.cache_clear()


async def test_create_host_ignores_pool_when_pool_size_is_zero(monkeypatch):
    # Default settings: pool_size = 0.
    get_settings.cache_clear()
    await _seed_pool_host(name="ch-orphan-pool-host")
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    async with async_session_factory() as session:
        service = HostService(session)
        result = await service.get_or_create_host(env={}, image=None)

    assert result.claimed_at is None
    mocked_provision.assert_awaited_once()


async def test_pool_shed_skips_a_host_claimed_in_the_race(pooled_settings, monkeypatch):
    # A pool host claimed between the maintainer's excess selection and the
    # shed's locked delete must be left for its owner, not torn down.
    delete_vm = AsyncMock()
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", delete_vm)
    claimed = await _seed_pool_host(name="lb-claimed", claimed_at=utc_now())

    async with async_session_factory() as session:
        service = HostService(session, settings=pooled_settings)
        deleted = await service.delete_host(claimed.id, pool_shed=True)

    assert not deleted
    delete_vm.assert_not_awaited()
    async with async_session_factory() as session:
        assert await session.get(Host, claimed.id) is not None
