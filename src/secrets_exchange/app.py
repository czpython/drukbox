from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from host_secrets import catalog
from host_secrets.placeholder import Placeholder
from hosts.models import Host

app = FastAPI(title="Drukbox secrets exchange")

# Caddy copies these from an answer into the upstream request. deploy/caddy names them too.
UPSTREAM_HOST = "X-Upstream-Host"
UPSTREAM_HEADER = "X-Upstream-Header"
UPSTREAM_CREDENTIAL = "X-Upstream-Credential"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/authorize")
async def authorize(
    session: Annotated[AsyncSession, Depends(get_session)],
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

    if "value" not in entry:
        # The refresh loop serves source entries. It is not in this release.
        return Response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers={"Retry-After": "30"}
        )

    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            UPSTREAM_HOST: service["host"],
            UPSTREAM_HEADER: service["credential_header"],
            UPSTREAM_CREDENTIAL: f"{service['credential_prefix']}{entry['value']}",
        },
    )
