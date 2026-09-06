from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from host_secrets import catalog
from host_secrets.placeholder import Placeholder
from hosts.models import Host
from secrets_exchange.secrets import Secrets, SourceUnavailableError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(timeout=10) as client:
        app.state.secrets = Secrets(client)
        yield


app = FastAPI(title="Drukbox secrets exchange", lifespan=lifespan)

# Caddy copies these from an answer into the upstream request. deploy/caddy names them too.
UPSTREAM_HOST = "X-Upstream-Host"
UPSTREAM_HEADER = "X-Upstream-Header"
UPSTREAM_CREDENTIAL = "X-Upstream-Credential"


def get_secrets(request: Request) -> Secrets:
    return request.app.state.secrets


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/authorize")
async def authorize(
    session: Annotated[AsyncSession, Depends(get_session)],
    secrets: Annotated[Secrets, Depends(get_secrets)],
    authorization: Annotated[str, Header()] = "",
    x_forwarded_uri: Annotated[str, Header()] = "",
) -> Response:
    """Answer Caddy's forward_auth with the upstream and the real credential.

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

    service = catalog.service(placeholder.service, entry)
    if not x_forwarded_uri.startswith(f"/{service['host']}/"):
        raise HTTPException(status.HTTP_403_FORBIDDEN)

    try:
        secret = await secrets.current(host.id, placeholder.service, entry)
    except SourceUnavailableError:
        return Response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers={"Retry-After": "5"}
        )

    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            UPSTREAM_HOST: service["host"],
            UPSTREAM_HEADER: service["credential_header"],
            UPSTREAM_CREDENTIAL: f"{service['credential_prefix']}{secret.value}",
        },
    )
