import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from core.database import async_session_factory
from host_secrets.placeholder import Placeholder
from hosts.models import Host
from hosts.service import utc_now
from secrets_exchange.app import UPSTREAM_CREDENTIAL, UPSTREAM_HEADER, UPSTREAM_HOST, app
from secrets_exchange.secrets import Secrets


@pytest.fixture
async def edge() -> AsyncGenerator[AsyncClient]:
    # ASGITransport does not run the lifespan, so the test gives the app its secrets.
    async with (
        httpx.AsyncClient() as outbound,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://secrets exchange") as c,
    ):
        app.state.secrets = Secrets(outbound)
        yield c


async def test_a_placeholder_is_exchanged_for_the_real_secret(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    await _create_host(
        host_id, {"github": {"value": "ghs_real", "placeholder_fingerprint": minted.fingerprint}}
    )

    response = await edge.get("/authorize", headers=_headers(str(minted), "/api.github.com/user"))

    assert response.status_code == 200
    assert response.headers["X-Upstream-Host"] == "api.github.com"
    assert response.headers["X-Upstream-Header"] == "Authorization"
    assert response.headers["X-Upstream-Credential"] == "Bearer ghs_real"


async def test_a_custom_service_gets_its_own_header_shape(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "acme")
    await _create_host(
        host_id,
        {
            "acme": {
                "host": "api.acme.test",
                "credential_header": "x-api-key",
                "credential_prefix": "",
                "credential_var": "ACME_TOKEN",
                "endpoint_var": "",
                "base_path": "",
                "value": "ak_live",
                "placeholder_fingerprint": minted.fingerprint,
            }
        },
    )

    response = await edge.get("/authorize", headers=_headers(str(minted), "/api.acme.test/v1"))

    assert response.status_code == 200
    assert response.headers["X-Upstream-Host"] == "api.acme.test"
    assert response.headers["X-Upstream-Header"] == "x-api-key"
    assert response.headers["X-Upstream-Credential"] == "ak_live"


@respx.mock
async def test_an_issuer_entry_is_fetched_and_exchanged(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    issuer = {"url": "https://mint.test/x", "headers": {"X-Key": "k"}, "refresh": "1h"}
    await _create_host(
        host_id, {"github": {"issuer": issuer, "placeholder_fingerprint": minted.fingerprint}}
    )
    route = respx.get("https://mint.test/x").respond(json={"value": "ghs_minted"})

    response = await edge.get("/authorize", headers=_headers(str(minted), "/api.github.com/user"))

    assert response.status_code == 200
    assert response.headers["X-Upstream-Credential"] == "Bearer ghs_minted"
    assert route.calls[0].request.headers["X-Key"] == "k"


@respx.mock
async def test_an_issuer_that_gives_nothing_usable_answers_503(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    issuer = {"url": "https://mint.test/x", "headers": {"X-Key": "k"}, "refresh": "1h"}
    await _create_host(
        host_id, {"github": {"issuer": issuer, "placeholder_fingerprint": minted.fingerprint}}
    )
    respx.get("https://mint.test/x").respond(status_code=502)

    response = await edge.get("/authorize", headers=_headers(str(minted), "/api.github.com/user"))

    assert response.status_code == 503
    assert response.headers["Retry-After"]
    assert "X-Upstream-Credential" not in response.headers


@pytest.mark.parametrize(
    ("placeholder", "uri"),
    [
        ("", "/api.github.com/user"),
        ("sk-ant-oat01-something", "/api.github.com/user"),
        ("drk.{host}.github.wrong-secret", "/api.github.com/user"),
        ("drk.{other}.github.{secret}", "/api.github.com/user"),
        ("drk.{host}.openai.{secret}", "/api.openai.com/v1/responses"),
        ("drk.{host}.github.{secret}", "/api.anthropic.com/v1/messages"),
        ("drk.{host}.github.{secret}", "/api.github.com"),
    ],
)
async def test_anything_else_is_refused_with_403_never_401(
    edge, placeholder: str, uri: str
) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    await _create_host(
        host_id, {"github": {"value": "ghs_real", "placeholder_fingerprint": minted.fingerprint}}
    )
    presented = placeholder.format(host=host_id.hex, other=uuid.uuid4().hex, secret=minted.secret)

    response = await edge.get("/authorize", headers=_headers(presented, uri))

    assert response.status_code == 403
    assert "X-Upstream-Credential" not in response.headers


async def test_healthz(edge) -> None:
    assert (await edge.get("/healthz")).status_code == 200


def test_the_caddy_snippet_reads_the_headers_the_exchange_answers_with() -> None:
    snippet = (
        Path(__file__).parents[3] / "deploy" / "caddy" / "secrets_exchange.caddy"
    ).read_text()

    assert f"copy_headers {UPSTREAM_HOST} {UPSTREAM_HEADER} {UPSTREAM_CREDENTIAL}" in snippet
    assert "(secrets_exchange) {" in snippet


def _headers(placeholder: str, uri: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {placeholder}", "X-Forwarded-Uri": uri}


async def _create_host(host_id: uuid.UUID, secrets: dict[str, object]) -> None:
    now = utc_now()
    async with async_session_factory() as session:
        session.add(
            Host(
                id=host_id,
                name=f"sb-{host_id.hex[:12]}",
                image="sandbox:latest",
                secrets=secrets,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
