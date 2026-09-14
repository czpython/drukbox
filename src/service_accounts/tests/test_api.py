from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from api.app import app
from core.database import Base, async_session_factory, engine
from service_accounts.models import ServiceAccount

ADMIN = {"Authorization": "Bearer service-token"}


async def create(client: AsyncClient, name: str = "account-a") -> str:
    response = await client.post("/service-accounts", json={"name": name}, headers=ADMIN)
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()["token"]


async def test_create_stores_only_name_and_fingerprint(client: AsyncClient) -> None:
    token = await create(client)

    async with async_session_factory() as session:
        stored = await session.get_one(ServiceAccount, "account-a")
        assert stored.name == "account-a"
        assert stored.fingerprint == ServiceAccount.get_fingerprint(token)
        assert set(ServiceAccount.__table__.columns.keys()) == {"name", "fingerprint"}

    duplicate = await client.post("/service-accounts", json={"name": "account-a"}, headers=ADMIN)
    assert duplicate.status_code == 409
    assert duplicate.json()["error_code"] == "SERVICE_ACCOUNT_EXISTS"
    assert token not in duplicate.text
    assert (await client.get("/service-accounts", headers=ADMIN)).status_code == 405
    assert (await client.get("/service-accounts/account-a", headers=ADMIN)).status_code == 405


@pytest.mark.parametrize("name", ["", "-a", "Account-A", "has space", "with/slash", "a" * 65])
async def test_create_rejects_invalid_names(client: AsyncClient, name: str) -> None:
    response = await client.post("/service-accounts", json={"name": name}, headers=ADMIN)
    assert response.status_code == 422


async def test_remove_rejects_invalid_name(client: AsyncClient) -> None:
    await create(client)
    assert (await client.delete("/service-accounts/Account-A", headers=ADMIN)).status_code == 422


@pytest.mark.parametrize("use_admin", [False, True])
async def test_token_works_on_each_router(client: AsyncClient, use_admin: bool) -> None:
    token = "service-token" if use_admin else await create(client)
    headers = {"Authorization": f"Bearer {token}"}

    with (
        patch("providers.exe.provider.ExeProvider.diagnose", new=AsyncMock(return_value="exe ok")),
        patch("networking.tailscale.Tailscale.diagnose", new=AsyncMock(return_value="tailnet ok")),
        patch(
            "secrets_exchange.client.SecretsExchange.diagnose",
            new=AsyncMock(return_value="exchange healthy"),
        ),
        patch(
            "http_proxies.service.HTTPProxyService.create_http_proxy", new=AsyncMock()
        ) as create_proxy,
    ):
        for path in ("/hosts", "/templates", "/doctor"):
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            assert token not in response.text

        response = await client.post(
            "/http-proxies",
            json={
                "name": "test-proxy",
                "target": "https://example.com",
                "headers": {"Authorization": "Bearer upstream-token"},
            },
            headers=headers,
        )
        assert response.status_code == 201
        assert token not in response.text
        create_proxy.assert_awaited_once()


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "issued"])
async def test_only_admin_can_manage_service_accounts(
    client: AsyncClient, authorization: str | None
) -> None:
    if authorization == "issued":
        authorization = f"Bearer {await create(client)}"

    headers = {"Authorization": authorization} if authorization else {}
    expected = 403 if authorization else 401
    response = await client.post("/service-accounts", json={"name": "account-b"}, headers=headers)
    assert response.status_code == expected
    response = await client.delete("/service-accounts/account-a", headers=headers)
    assert response.status_code == expected


async def test_non_ascii_bearer_is_rejected(client: AsyncClient) -> None:
    headers = {b"Authorization": "Bearer tökén".encode("latin-1")}
    assert (await client.get("/hosts", headers=headers)).status_code == 403


async def test_removal_rejects_every_protected_route_without_restart(client: AsyncClient) -> None:
    token = await create(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/hosts", headers=headers)).status_code == 200
    response = await client.delete("/service-accounts/account-a", headers=ADMIN)
    assert response.status_code == 204
    assert not response.content

    # Auth runs before path parsing, so the templated paths serve as they are.
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 403, (method, path, response.text)
            assert token not in response.text

    replacement = await create(client)
    assert replacement != token
    assert (await client.get("/hosts", headers=headers)).status_code == 403
    assert (
        await client.get("/hosts", headers={"Authorization": f"Bearer {replacement}"})
    ).status_code == 200


async def test_admin_account_has_no_token_and_cannot_be_removed(client: AsyncClient) -> None:
    response = await client.post("/service-accounts", json={"name": "admin"}, headers=ADMIN)
    assert response.status_code == 409
    response = await client.delete("/service-accounts/admin", headers=ADMIN)
    assert response.status_code == 409
    assert response.json()["error_code"] == "SERVICE_ACCOUNT_STATE"
    assert (await client.get("/hosts", headers={"Authorization": "Bearer "})).status_code == 401


async def test_remove_unknown_name(client: AsyncClient) -> None:
    response = await client.delete("/service-accounts/unknown", headers=ADMIN)
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"


async def test_lookup_failure_returns_503_and_keeps_admin_keys(client: AsyncClient) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.tables["service_accounts"].drop)

    assert (await client.get("/hosts", headers={"Authorization": "Bearer nope"})).status_code == 503
    assert (await client.get("/hosts", headers=ADMIN)).status_code == 200
