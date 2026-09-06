import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import async_session_factory, get_session
from host_secrets import catalog
from host_secrets.placeholder import Placeholder
from hosts.models import Host, HostStatus
from providers.registry import get_vm_provider
from secrets_exchange.secrets import IssuerUnavailableError, Secrets

logger = logging.getLogger(__name__)

# How often the timer looks for a held value that nears its end.
TICK = timedelta(seconds=5)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(timeout=10) as client:
        app.state.secrets = Secrets(client)
        timer = asyncio.create_task(push_on_expiry(app.state.secrets))
        try:
            yield
        finally:
            timer.cancel()


async def push_on_expiry(secrets: Secrets) -> None:
    """Providers that hold the value get each issuer-backed one again before it expires.

    Proxy providers are not visited. Their value lives here and refreshes on request.
    """
    while True:
        try:
            await push_held(secrets)
        except Exception:
            # A timer that dies would let every held value expire. Log, keep the timer.
            logger.exception("push timer failed")
        await asyncio.sleep(TICK.total_seconds())


async def push_held(secrets: Secrets) -> None:
    """One pass over the active hosts, side by side, so one slow issuer delays no other host."""
    async with async_session_factory() as session:
        active = select(Host).where(Host.status == HostStatus.ACTIVE.value)
        hosts = (await session.execute(active)).scalars().all()
    await asyncio.gather(*(push_host(secrets, host) for host in hosts))


async def push_host(secrets: Secrets, host: Host) -> None:
    """Every issuer-backed entry of one host, when its provider holds the value."""
    try:
        injection = get_vm_provider(host.provider).secret_injection
        if injection.holds_value:
            for service, entry in host.secrets.items():
                if "issuer" in entry:
                    await secrets.push(host.id, host.name, service, entry, injection)
    except Exception:
        # One host's trouble must not cost the others their values.
        logger.exception("push for host %s failed", host.name)


app = FastAPI(title="Drukbox secrets exchange", lifespan=lifespan)

# The proxy reads these from an answer and swaps the header. deploy/proxy/swap.py names them too.
UPSTREAM_HOST = "X-Upstream-Host"
UPSTREAM_HEADER = "X-Upstream-Header"
UPSTREAM_CREDENTIAL = "X-Upstream-Credential"


def get_secrets(request: Request) -> Secrets:
    return request.app.state.secrets


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/upstreams")
async def upstreams(session: Annotated[AsyncSession, Depends(get_session)]) -> list[str]:
    """The hosts with a registered secret. The proxy terminates TLS for these only."""
    hosts = (await session.execute(select(Host))).scalars()
    return sorted(
        {
            upstream.host
            for host in hosts
            for name, entry in host.secrets.items()
            for upstream in catalog.service(name, entry).upstreams
        }
    )


@app.get("/authorize")
async def authorize(
    session: Annotated[AsyncSession, Depends(get_session)],
    secrets: Annotated[Secrets, Depends(get_secrets)],
    authorization: Annotated[str, Header()] = "",
    x_forwarded_host: Annotated[str, Header()] = "",
) -> Response:
    """Answer the proxy with the upstream and the real credential for a placeholder
    sent to ``x_forwarded_host``.

    Never answer 401. git answers a 401 with a retry through its own credential store.
    """
    try:
        placeholder = Placeholder.read(authorization)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN) from None

    host = await session.get(Host, placeholder.host_id)
    if not host or placeholder.service not in host.secrets:
        raise HTTPException(status.HTTP_403_FORBIDDEN)

    entry = host.secrets[placeholder.service]
    if not placeholder.matches(entry["placeholder_fingerprint"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN)

    upstreams = {
        upstream.host: upstream
        for upstream in catalog.service(placeholder.service, entry).upstreams
    }
    if x_forwarded_host not in upstreams:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    upstream = upstreams[x_forwarded_host]

    try:
        secret = await secrets.current(host.id, placeholder.service, entry)
    except IssuerUnavailableError:
        return Response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers={"Retry-After": "5"}
        )

    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            UPSTREAM_HOST: upstream.host,
            UPSTREAM_HEADER: upstream.header,
            UPSTREAM_CREDENTIAL: upstream.credential(secret.value),
        },
    )
