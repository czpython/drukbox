import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, or_, select

from core.database import async_session_factory
from core.settings import Settings, get_settings
from hosts.models import Host, HostStatus
from hosts.service import HostService, utc_now
from networking.tailscale import Tailscale

logger = logging.getLogger(__name__)

# Bail out after this many consecutive create failures (broker outage scenario)
# rather than logging the same failure once per deficit slot per tick.
_MAX_CONSECUTIVE_CREATE_FAILURES = 3


@dataclass(frozen=True)
class PoolMaintenanceSummary:
    created: int = 0
    removed_excess: int = 0
    targets: dict[str, int] = field(default_factory=dict)


async def maintain_pool() -> PoolMaintenanceSummary:
    """Top up / shrink each provider's warm-host pool and reap failed pool members.

    Each tick creates at most ``POOL_MAX_CREATES_PER_TICK`` hosts across all
    providers. Overlapping ticks or accidental scheduler replicas can therefore
    over-provision by at most that batch size, which the next tick sheds via the
    excess path. This intentionally avoids any cross-tick locking — see
    CONTRIBUTING for context.
    """
    settings = get_settings()
    targets = settings.get_pool_targets()
    if not targets:
        return PoolMaintenanceSummary()
    tailscale: Tailscale | None = Tailscale.from_settings() if settings.tailscale_enabled else None
    try:
        return await _maintain(settings, targets, tailscale)
    finally:
        if tailscale is not None:
            await tailscale.aclose()


async def _maintain(
    settings: Settings, targets: dict[str, int], tailscale: Tailscale | None
) -> PoolMaintenanceSummary:
    now = utc_now()

    async with async_session_factory() as session:
        # Count warm pool members per provider that are still fresh and
        # unclaimed. Only pool_member hosts count — demand-provisioned hosts
        # also keep claimed_at NULL but must never be counted or shed. Exclude
        # hosts whose expires_at has already passed — those are queued for
        # janitor reaping.
        counted = await session.execute(
            select(Host.provider, func.count())
            .where(Host.pool_member.is_(True))
            .where(Host.claimed_at.is_(None))
            .where(Host.status != HostStatus.ERROR.value)
            .where(or_(Host.expires_at.is_(None), Host.expires_at > now))
            .group_by(Host.provider)
        )
        current: dict[str, int] = {provider: count for provider, count in counted.all()}

        excess_ids: list[uuid.UUID] = []
        # Sweep the union of configured and present providers: one dropped from
        # the targets sheds to zero instead of idling until its max-age TTL.
        for provider in sorted(current.keys() | targets.keys()):
            shed = current.get(provider, 0) - targets.get(provider, 0)
            if shed <= 0:
                continue
            excess_ids.extend(
                (
                    await session.execute(
                        select(Host.id)
                        .where(Host.provider == provider)
                        .where(Host.pool_member.is_(True))
                        .where(Host.claimed_at.is_(None))
                        .where(Host.status != HostStatus.ERROR.value)
                        .where(or_(Host.expires_at.is_(None), Host.expires_at > now))
                        .order_by(Host.created_at.asc())
                        .limit(shed)
                    )
                ).scalars()
            )

    underfilled = [
        provider for provider, target in targets.items() if target > current.get(provider, 0)
    ]
    # Spread the per-tick budget round-robin across providers so one large
    # deficit can't starve the others. The order is shuffled each tick: with
    # no cross-tick state, a fixed order would starve the last provider
    # whenever the cap is smaller than the number of underfilled providers —
    # permanently so when an earlier provider's creates keep failing.
    random.shuffle(underfilled)
    deficits = {provider: targets[provider] - current.get(provider, 0) for provider in underfilled}
    batch: list[str] = []
    while deficits and len(batch) < settings.pool_max_creates_per_tick:
        for provider in list(deficits):
            if len(batch) == settings.pool_max_creates_per_tick:
                break
            batch.append(provider)
            deficits[provider] -= 1
            if not deficits[provider]:
                del deficits[provider]

    created = 0
    consecutive_failures = 0
    for provider in batch:
        try:
            async with async_session_factory() as session:
                service = HostService(session, settings=settings, tailscale=tailscale)
                expires_at = utc_now() + timedelta(hours=settings.pool_host_max_age_hours)
                await service.create_host(
                    env={},
                    image=None,
                    expires_at=expires_at,
                    provider=provider,
                    pool_member=True,
                )
                created += 1
                consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            logger.exception("pool: failed to create pool host provider=%s", provider)
            if consecutive_failures >= _MAX_CONSECUTIVE_CREATE_FAILURES:
                logger.error(
                    "pool: aborting top-up after %d consecutive failures",
                    consecutive_failures,
                )
                break

    removed_excess = 0
    for host_id in excess_ids:
        try:
            async with async_session_factory() as session:
                service = HostService(session, settings=settings, tailscale=tailscale)
                if await service.delete_host(host_id, pool_shed=True):
                    removed_excess += 1
        except Exception:
            logger.exception("pool: failed to shed excess pool host_id=%s", host_id)

    if created or removed_excess:
        logger.info(
            "pool: maintained (created=%d, removed_excess=%d, targets=%s)",
            created,
            removed_excess,
            targets,
        )

    return PoolMaintenanceSummary(
        created=created,
        removed_excess=removed_excess,
        targets=targets,
    )


if __name__ == "__main__":
    # Cron entry point: `python -m hosts.pool`.
    import asyncio

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(maintain_pool())
