from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from hosts.service import HostService


async def get_host_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HostService:
    # The lifespan installs a process-wide Tailscale client on app.state so
    # every request shares one httpx connection pool + OAuth token cache.
    # Test harnesses that bypass lifespan (ASGITransport) leave the slot
    # empty, and HostService builds its own.
    return HostService(session, tailscale=getattr(request.app.state, "tailscale", None))
