import hmac
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Response, status

from core.database import async_session_factory
from host_secrets.catalog import describe
from host_secrets.placeholder import digest, parse
from hosts.models import Host

app = FastAPI(title="Drukbox secrets exchange")

# What Caddy copies from an answer into the upstream request; deploy/caddy reads the same names.
UPSTREAM_HOST = "X-Upstream-Host"
UPSTREAM_HEADER = "X-Upstream-Header"
UPSTREAM_CREDENTIAL = "X-Upstream-Credential"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/authorize")
async def authorize(
    authorization: Annotated[str, Header()] = "",
    x_forwarded_uri: Annotated[str, Header()] = "",
) -> Response:
    """Answer Caddy's forward_auth: where the request goes and the real credential it carries.

    Never 401: git answers a 401 by retrying with its own credential machinery.
    """
    try:
        host_id, name, secret = parse(authorization.removeprefix("Bearer "))
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN) from None

    async with async_session_factory() as session:
        host = await session.get(Host, host_id)
    if not host or name not in host.secrets:
        raise HTTPException(status.HTTP_403_FORBIDDEN)

    entry = host.secrets[name]
    if not hmac.compare_digest(digest(secret), entry.get("placeholder_sha256", "")):
        raise HTTPException(status.HTTP_403_FORBIDDEN)

    service = describe(name, entry)
    if not x_forwarded_uri.startswith(f"/{service['host']}/"):
        raise HTTPException(status.HTTP_403_FORBIDDEN)

    if "value" not in entry:
        # A source entry is served from the refresh loop's cache, which lands separately.
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
