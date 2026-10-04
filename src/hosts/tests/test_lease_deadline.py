from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from core.database import async_session_factory
from hosts.exceptions import HostLeaseError
from hosts.models import Host, HostStatus
from hosts.service import HostService

AUTH = {"Authorization": "Bearer service-token"}


@pytest.fixture
def limited_provider(stub_provider, monkeypatch):
    stub_provider.max_lifetime = timedelta(minutes=44)
    monkeypatch.setattr(HostService, "provision", AsyncMock())
    return stub_provider


async def test_create_caps_default_lease_and_stores_deadline(client: AsyncClient, limited_provider):
    response = await client.post("/hosts", headers=AUTH, json={"provider": "stub"})
    assert response.status_code == 201
    body = response.json()
    assert body["expires_at"] == body["lease_deadline"]
    assert datetime.fromisoformat(body["lease_deadline"]) == datetime.fromisoformat(
        body["created_at"]
    ) + timedelta(minutes=44)
    async with async_session_factory() as session:
        host = await session.get(Host, UUID(body["id"]))
        assert host
        assert host.expires_at == host.lease_deadline


@pytest.mark.parametrize("expiry", [None, (datetime.now(UTC) + timedelta(days=1)).isoformat()])
async def test_unavailable_lease_is_rejected_before_a_host_row(
    client: AsyncClient, limited_provider, expiry
):
    response = await client.post(
        "/hosts", headers=AUTH, json={"provider": "stub", "expires_at": expiry}
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "HOST_LEASE"
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Host)) == 0


async def test_explicit_short_lease_is_preserved(client: AsyncClient, limited_provider):
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    response = await client.post(
        "/hosts", headers=AUTH, json={"provider": "stub", "expires_at": expiry.isoformat()}
    )
    assert response.status_code == 201
    assert datetime.fromisoformat(response.json()["expires_at"]) == expiry


async def test_renew_uses_stored_deadline_after_provider_setting_changes(
    client: AsyncClient, limited_provider
):
    response = await client.post("/hosts", headers=AUTH, json={"provider": "stub"})
    body = response.json()
    async with async_session_factory() as session:
        host = await session.get(Host, UUID(body["id"]))
        assert host
        host.status = HostStatus.ACTIVE.value
        await session.commit()
    limited_provider.max_lifetime = timedelta(days=1)
    renewed = await client.post(f"/hosts/{body['id']}/renew", headers=AUTH, json={})
    assert renewed.status_code == 200
    assert renewed.json()["expires_at"] == body["lease_deadline"]
    rejected = await client.post(
        f"/hosts/{body['id']}/renew",
        headers=AUTH,
        json={"expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )
    assert rejected.status_code == 400
    assert rejected.json()["error_code"] == "HOST_LEASE"


async def test_warm_pool_expiry_and_claim_stay_within_lifetime(limited_provider):
    async with async_session_factory() as session:
        service = HostService(session)
        warm = await service.create_host(
            env={},
            image=None,
            provider="stub",
            pool_member=True,
            expires_at=datetime.now(UTC) + timedelta(hours=4),
        )
        assert warm.expires_at == warm.lease_deadline
        warm.status = HostStatus.ACTIVE.value
        await session.commit()
        claimed = await service._try_claim_pool_host(
            service_account="admin", provider="stub", expires_at=...
        )
        assert claimed
        assert claimed.id == warm.id
        assert claimed.expires_at == warm.lease_deadline
        await service._release_idempotency_loser(claimed)
        await session.refresh(warm)
        assert not warm.claimed_at
        assert warm.expires_at == warm.lease_deadline


@pytest.mark.parametrize("expiry", [None, datetime.now(UTC) + timedelta(hours=1)])
async def test_pool_does_not_claim_a_host_that_cannot_meet_requested_lease(
    limited_provider, expiry
):
    async with async_session_factory() as session:
        service = HostService(session)
        warm = await service.create_host(env={}, image=None, provider="stub", pool_member=True)
        warm.status = HostStatus.ACTIVE.value
        await session.commit()
        claimed = await service._try_claim_pool_host(
            service_account="admin", provider="stub", expires_at=expiry
        )
        assert not claimed
        await session.refresh(warm)
        assert not warm.claimed_at


def test_expired_lifetime_cannot_be_renewed():
    now = datetime.now(UTC)
    host = Host(lease_deadline=now - timedelta(seconds=1))
    with pytest.raises(HostLeaseError, match="lifetime has ended"):
        host.lease_expiry(..., default=now + timedelta(hours=1))


async def test_provisioning_past_the_lifetime_marks_host_failed(
    client: AsyncClient, limited_provider, monkeypatch
):
    async def provision(service: HostService, host_id: str) -> None:
        host = await service.get_host(UUID(host_id))
        assert host
        host.lease_deadline = datetime.now(UTC) - timedelta(seconds=1)
        host.status = HostStatus.ACTIVE.value
        await service.session.commit()

    monkeypatch.setattr(HostService, "provision", provision)
    response = await client.post("/hosts", headers=AUTH, json={"provider": "stub"})
    assert response.status_code == 502
    async with async_session_factory() as session:
        host = (await session.execute(select(Host))).scalar_one()
        assert host.status == HostStatus.ERROR.value
        assert host.expires_at
        assert host.expires_at <= datetime.now(UTC)
