from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.settings import get_settings
from hosts.service import HostService
from networking.tailscale import Tailscale
from secret_proxy.tunnels import ReverseTunnelManager


async def get_host_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HostService:
    # The lifespan installs a process-wide Tailscale client on app.state so
    # every request shares one httpx connection pool + OAuth token cache.
    # Test harnesses that bypass lifespan (ASGITransport) lazily get their
    # own here; class-level patches on Tailscale still apply. With
    # tailscale_enabled=False the slot stays None and HostService skips the
    # network layer entirely.
    tailscale: Tailscale | None = getattr(request.app.state, "tailscale", None)
    if not tailscale and get_settings().tailscale_enabled:
        tailscale = Tailscale.from_settings()
        request.app.state.tailscale = tailscale
    reverse_tunnels: ReverseTunnelManager | None = getattr(
        request.app.state, "reverse_tunnels", None
    )
    return HostService(
        session,
        tailscale=tailscale,
        reverse_tunnels=reverse_tunnels,
    )
