import logging
import uuid

from sqlalchemy import select

from core.database import async_session_factory
from core.exceptions import ResourceNotFoundError
from core.settings import get_settings
from hosts.models import Host
from hosts.service import HostService, utc_now
from networking.tailscale import Tailscale

logger = logging.getLogger(__name__)


async def reap_expired_hosts() -> list[uuid.UUID]:
    """Delete hosts whose ``expires_at`` has passed.

    Iterates candidate ids without holding row locks; each delete acquires its
    own per-row lock through HostService.delete_host and re-validates the lease
    there (expired_only), so a renewal that lands after selection spares the
    host. Rows still in an early provisioning state are force-reaped: their
    expires_at is the safety TTL (created + provisioning_grace_seconds), so an
    expired one means provisioning was abandoned — a live provision's TTL is
    still in the future. The force path tears down any VM partial provisioning
    may have created.
    """
    async with async_session_factory() as session:
        now = utc_now()
        result = await session.execute(
            select(Host.id).where(Host.expires_at.is_not(None)).where(Host.expires_at < now)
        )
        expired_ids = list(result.scalars())

    reaped: list[uuid.UUID] = []
    tailscale: Tailscale | None = (
        Tailscale.from_settings() if get_settings().tailscale_enabled else None
    )

    try:
        for host_id in expired_ids:
            try:
                async with async_session_factory() as session:
                    deleted = await HostService(session, tailscale=tailscale).delete_host(
                        host_id, force=True, expired_only=True
                    )
            except ResourceNotFoundError:
                # Another janitor cycle, or an explicit DELETE, already removed it.
                continue
            except Exception:
                logger.exception("janitor: failed to reap expired host: host_id=%s", host_id)
                continue
            else:
                if not deleted:
                    # Renewed between selection and the locked delete — spared.
                    continue
                logger.info("janitor: reaped expired host: host_id=%s", host_id)
                reaped.append(host_id)
    finally:
        if tailscale is not None:
            await tailscale.aclose()

    return reaped


if __name__ == "__main__":
    # Cron entry point: `python -m hosts.janitor`.
    import asyncio

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(reap_expired_hosts())
