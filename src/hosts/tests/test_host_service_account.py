from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from core.database import async_session_factory
from core.settings import get_settings
from hosts.models import HostStatus
from hosts.service import HostService

ADMIN = {"Authorization": "Bearer service-token"}


@pytest.fixture(autouse=True)
def provision(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    provision = AsyncMock()
    monkeypatch.setattr(HostService, "provision", provision)
    return provision


async def issue(client: AsyncClient, name: str) -> dict[str, str]:
    response = await client.post("/service-accounts", json={"name": name}, headers=ADMIN)
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def test_host_records_its_service_account(client: AsyncClient) -> None:
    account = await issue(client, "account-a")
    created = await client.post("/hosts", json={"service_account": "forged"}, headers=account)
    assert created.status_code == 201
    assert created.json()["service_account"] == "account-a"
    account_host = created.json()["id"]

    created = await client.post("/hosts", headers=ADMIN)
    assert created.json()["service_account"] == "admin"
    admin_host = created.json()["id"]

    listed = await client.get("/hosts", headers=account)
    assert {host["id"]: host["service_account"] for host in listed.json()} == {
        account_host: "account-a",
        admin_host: "admin",
    }

    assert (await client.delete("/service-accounts/account-a", headers=ADMIN)).status_code == 204
    fetched = await client.get(f"/hosts/{account_host}", headers=ADMIN)
    assert fetched.json()["service_account"] == "account-a"


@pytest.mark.parametrize("name", ["admin", "account-a"])
async def test_pool_claim_records_service_account_and_release_clears_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, name: str, provision: AsyncMock
) -> None:
    monkeypatch.setattr(get_settings(), "pool_size", 1)

    async with async_session_factory() as session:
        service = HostService(session)
        pool_host = await service.create_host(env={}, image=None, pool_member=True)
        assert pool_host.service_account is None
        pool_host.status = HostStatus.ACTIVE.value
        await session.commit()
        pool_id = pool_host.id

    headers = ADMIN if name == "admin" else await issue(client, name)
    claimed = await client.post("/hosts", headers=headers)
    assert claimed.status_code == 201
    assert claimed.json()["id"] == str(pool_id)
    assert claimed.json()["service_account"] == name
    provision.assert_awaited_once()

    async with async_session_factory() as session:
        await HostService(session)._release_idempotency_loser(pool_host)

    released = await client.get(f"/hosts/{pool_id}", headers=headers)
    assert released.json()["service_account"] is None


async def test_idempotency_key_belongs_to_the_caller_that_used_it(client: AsyncClient) -> None:
    account = await issue(client, "account-a")
    key = {"Idempotency-Key": "shared-retry"}
    first = await client.post("/hosts", headers={**account, **key})
    retry = await client.post("/hosts", headers={**account, **key})
    assert retry.json()["id"] == first.json()["id"]

    replay = await client.post("/hosts", headers={**ADMIN, **key})
    assert replay.status_code == 409
    assert replay.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"
