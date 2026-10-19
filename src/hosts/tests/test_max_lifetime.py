from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import AsyncClient

from core.database import async_session_factory
from hosts.models import Host, HostStatus
from hosts.service import HostService, utc_now

AUTH = {"Authorization": "Bearer service-token"}
LIFETIME = timedelta(minutes=44)


@pytest.fixture
def limited_provider(stub_provider, monkeypatch):
    stub_provider.max_lifetime = LIFETIME
    monkeypatch.setattr(HostService, "provision", AsyncMock())
    return stub_provider


async def activate(host_id: UUID) -> None:
    async with async_session_factory() as session:
        host = await session.get(Host, host_id)
        assert host
        host.status = HostStatus.ACTIVE.value
        await session.commit()


@pytest.mark.parametrize(
    "lease",
    [{}, {"expires_at": None}, {"expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}],
    ids=["default", "permanent", "later"],
)
async def test_lease_ends_when_the_provider_stops_the_vm(
    client: AsyncClient, limited_provider, lease
):
    response = await client.post("/hosts", headers=AUTH, json={"provider": "stub"} | lease)
    body = response.json()
    assert datetime.fromisoformat(body["expires_at"]) == (
        datetime.fromisoformat(body["created_at"]) + LIFETIME
    )


@pytest.mark.parametrize(
    "renewal",
    [{}, {"expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}],
    ids=["default", "later"],
)
async def test_renewal_ends_when_the_provider_stops_the_vm(
    client: AsyncClient, limited_provider, renewal
):
    soon = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    body = (
        await client.post("/hosts", headers=AUTH, json={"provider": "stub", "expires_at": soon})
    ).json()
    await activate(UUID(body["id"]))

    renewed = await client.post(f"/hosts/{body['id']}/renew", headers=AUTH, json=renewal)
    assert datetime.fromisoformat(renewed.json()["expires_at"]) == (
        datetime.fromisoformat(body["created_at"]) + LIFETIME
    )


async def test_pool_leases_end_when_the_provider_stops_the_vm(limited_provider):
    async with async_session_factory() as session:
        service = HostService(session)
        warm = await service.create_host(
            env={},
            image=None,
            expires_at=utc_now() + timedelta(hours=4),
            provider="stub",
            pool_member=True,
        )
        stop = warm.created_at + LIFETIME
        assert warm.expires_at == stop
        await activate(warm.id)

        late = stop + timedelta(minutes=1)
        assert not await service._try_claim_pool_host(provider="stub", expires_at=late)
        claimed = await service._try_claim_pool_host(provider="stub")
        assert claimed
        assert claimed.expires_at == stop

        await service._release_idempotency_loser(claimed)
        await session.refresh(warm)
        assert not warm.claimed_at
        assert warm.expires_at == stop
