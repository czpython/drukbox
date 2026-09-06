import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy_encrypted_field import SecretDecryptError

from core.database import async_session_factory, get_session
from host_secrets import catalog
from host_secrets.placeholder import Placeholder
from hosts.models import Host, HostStatus
from providers.exceptions import ProviderError
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
        timer.add_done_callback(log_stop)
        try:
            yield
        finally:
            timer.cancel()


def log_stop(timer: asyncio.Task[None]) -> None:
    if not timer.cancelled() and (failure := timer.exception()):
        logger.error("the push timer stopped", exc_info=failure)


async def push_on_expiry(secrets: Secrets) -> None:
    """Proxy providers are not visited. Their value refreshes on request."""
    while True:
        try:
            await push_active_hosts(secrets)
        except SQLAlchemyError as exc:
            logger.warning("the hosts could not be read: %s", exc)
        await asyncio.sleep(TICK.total_seconds())


async def push_active_hosts(secrets: Secrets) -> None:
    """Side by side, so one slow issuer delays no other host. A host that is
    gone is forgotten first."""
    async with async_session_factory() as session:
        hosts = (await session.execute(select(Host))).scalars().all()
    secrets.forget_deleted_hosts({host.id for host in hosts})
    active = [host for host in hosts if host.status == HostStatus.ACTIVE.value]
    await asyncio.gather(*(push_to_host(secrets, host) for host in active))


async def push_to_host(secrets: Secrets, host: Host) -> None:
    try:
        injection = get_vm_provider(host.provider).secrets
        entries = dict(host.secrets)
    except (ProviderError, SecretDecryptError) as exc:
        logger.error("push for host %s failed: %s", host.name, exc)
        return
    if injection.needs_value:
        for service, entry in entries.items():
            if "issuer" in entry:
                await secrets.push(host.id, host.name, service, entry, injection)


app = FastAPI(title="Drukbox secrets exchange", lifespan=lifespan)

# deploy/proxy/swap.py reads these.
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
    """The proxy terminates TLS for these only."""
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
    """Never 401: git answers a 401 with a retry through its own credential store."""
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
            UPSTREAM_HEADER: upstream.auth_header,
            UPSTREAM_CREDENTIAL: upstream.credential(secret.value),
        },
    )
