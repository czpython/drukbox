import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from core.database import async_session_factory
from host_secrets.exceptions import SecretRefreshError
from host_secrets.placeholder import Placeholder
from hosts.models import Host, HostStatus
from hosts.service import utc_now
from secrets_exchange.app import app as exchange_app
from secrets_exchange.client import SecretsExchange
from secrets_exchange.secrets import Secrets

ADMIN = {"Authorization": "Bearer service-token"}
ISSUER_URL = "https://issuer.test/github"
ISSUER: dict[str, object] = {"issuer": {"url": ISSUER_URL, "headers": {}, "refresh": "1h"}}
STATIC: dict[str, object] = {"value": "static-credential"}


async def create_host(entry: dict[str, object]) -> tuple[uuid.UUID, Placeholder]:
    host_id = uuid.uuid4()
    placeholder = Placeholder.mint(host_id, "github")

    async with async_session_factory() as session:
        session.add(
            Host(
                id=host_id,
                name=f"sb-{host_id.hex[:12]}",
                image="test:image",
                status=HostStatus.ACTIVE.value,
                secrets={"github": {**entry, "placeholder_fingerprint": placeholder.fingerprint}},
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    return host_id, placeholder


@pytest.fixture
def exchange(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    refresh = AsyncMock()
    monkeypatch.setattr(SecretsExchange, "refresh", refresh)
    return refresh


async def test_refresh_orders_the_exchange(client: AsyncClient, exchange: AsyncMock) -> None:
    host_id, _ = await create_host(ISSUER)
    response = await client.post(f"/hosts/{host_id}/secrets/github/refresh", headers=ADMIN)
    assert response.status_code == 204
    assert not response.content
    exchange.assert_awaited_once_with(host_id, "github")


@pytest.mark.parametrize(
    "entry, service, expected, error_code",
    [
        (None, "github", 404, "NOT_FOUND"),
        (ISSUER, "anthropic", 404, "NOT_FOUND"),
        (STATIC, "github", 409, "SECRET_STATIC"),
    ],
)
async def test_refresh_answers_before_the_exchange(
    client: AsyncClient,
    exchange: AsyncMock,
    entry: dict[str, object] | None,
    service: str,
    expected: int,
    error_code: str,
) -> None:
    host_id = (await create_host(entry))[0] if entry else uuid.uuid4()
    response = await client.post(f"/hosts/{host_id}/secrets/{service}/refresh", headers=ADMIN)
    assert response.status_code == expected
    assert response.json()["error_code"] == error_code
    exchange.assert_not_awaited()


async def test_refresh_failure_carries_retry_after(
    client: AsyncClient, exchange: AsyncMock
) -> None:
    exchange.side_effect = SecretRefreshError("secrets exchange unavailable")
    host_id, _ = await create_host(ISSUER)
    response = await client.post(f"/hosts/{host_id}/secrets/github/refresh", headers=ADMIN)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {
        "detail": "secrets exchange unavailable",
        "error_code": "SECRET_REFRESH",
    }


@pytest.mark.parametrize(
    "headers, expected", [({}, 401), ({"Authorization": "Bearer invalid"}, 403)]
)
async def test_refresh_requires_a_token(
    client: AsyncClient, exchange: AsyncMock, headers: dict[str, str], expected: int
) -> None:
    response = await client.post(f"/hosts/{uuid.uuid4()}/secrets/github/refresh", headers=headers)
    assert response.status_code == expected
    exchange.assert_not_awaited()


@pytest.fixture
async def live_exchange() -> AsyncIterator[respx.MockRouter]:
    async with httpx.AsyncClient() as issuer_client:
        exchange_app.state.secrets = Secrets(issuer_client)

        with respx.mock as router:
            router.route(url__startswith=SecretsExchange.from_settings().url).mock(
                side_effect=respx.ASGIHandler(exchange_app)
            )
            yield router


async def test_refresh_replaces_the_exchange_value(
    client: AsyncClient, live_exchange: respx.MockRouter
) -> None:
    host_id, placeholder = await create_host(ISSUER)
    issuer = live_exchange.get(ISSUER_URL).respond(json={"value": "credential-one"})
    headers = {"Authorization": f"Bearer {placeholder}", "X-Forwarded-Host": "api.github.com"}

    async with AsyncClient(
        transport=ASGITransport(app=exchange_app), base_url=SecretsExchange.from_settings().url
    ) as edge:
        response = await edge.get("/authorize", headers=headers)
        assert response.headers["X-Upstream-Credential"] == "Bearer credential-one"
        issuer.respond(json={"value": "credential-two"})
        response = await client.post(f"/hosts/{host_id}/secrets/github/refresh", headers=ADMIN)
        assert response.status_code == 204
        response = await edge.get("/authorize", headers=headers)
        assert response.headers["X-Upstream-Credential"] == "Bearer credential-two"

    assert issuer.call_count == 2
