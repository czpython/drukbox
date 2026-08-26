import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from uuid6 import uuid7

from core.database import async_session_factory
from core.settings import get_settings
from hosts.models import Host, HostStatus
from hosts.service import utc_now
from providers.exe.settings import ExeSettings
from templates.models import Template, TemplateStatus

AUTH_HEADERS = {"Authorization": "Bearer service-token"}
SETUP_SCRIPT_HASH = "a" * 64
SETUP_SCRIPT = "apt-get update && apt-get install -y nodejs"


async def create_template_record(
    *,
    provider: str = "exe",
    base_image: str = "base:image",
    setup_script_hash: str = SETUP_SCRIPT_HASH,
    image: str = "",
    status: str = TemplateStatus.AVAILABLE.value,
    last_error: str = "",
    created_at: datetime | None = None,
) -> Template:
    now = created_at or utc_now()
    template = Template(
        id=uuid7(),
        provider=provider,
        base_image=base_image,
        setup_script_hash=setup_script_hash,
        setup_script=SETUP_SCRIPT,
        label="",
        image=image,
        status=status,
        last_error=last_error,
        created_at=now,
        updated_at=now,
    )
    async with async_session_factory() as session:
        session.add(template)
        await session.commit()
        await session.refresh(template)
    return template


async def test_create_host_resolves_template_id(client, monkeypatch):
    """An available template ID becomes the stored image and records its use."""
    template = await create_template_record(image="derived:image-by-id")
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    before = utc_now()
    response = await client.post(
        "/hosts",
        headers=AUTH_HEADERS,
        json={"template": str(template.id)},
    )
    after = utc_now()

    assert response.status_code == 201
    assert response.json()["image"] == "derived:image-by-id"
    async with async_session_factory() as session:
        host = await session.get(Host, uuid.UUID(response.json()["id"]))
        used_template = await session.get(Template, template.id)
    assert host is not None
    assert host.image == "derived:image-by-id"
    assert used_template is not None
    assert used_template.last_used_at is not None
    assert before <= used_template.last_used_at <= after


async def test_create_host_rejects_building_template(client):
    """A building template returns a conflict naming its current status."""
    template = await create_template_record(status=TemplateStatus.BUILDING.value)

    response = await client.post(
        "/hosts",
        headers=AUTH_HEADERS,
        json={"template": str(template.id)},
    )

    assert response.status_code == 409
    assert str(template.id) in response.json()["detail"]
    assert TemplateStatus.BUILDING.value in response.json()["detail"]
    assert response.json()["error_code"] == "TEMPLATE_NOT_AVAILABLE"
    async with async_session_factory() as session:
        host_count = await session.scalar(select(func.count()).select_from(Host))
    assert host_count == 0


async def test_create_host_rejects_failed_template_with_last_error(client):
    """A failed template conflict includes both its status and build diagnostic."""
    template = await create_template_record(
        status=TemplateStatus.FAILED.value,
        last_error="ProviderTransportError: builder unavailable",
    )

    response = await client.post(
        "/hosts",
        headers=AUTH_HEADERS,
        json={"template": str(template.id)},
    )

    assert response.status_code == 409
    assert str(template.id) in response.json()["detail"]
    assert TemplateStatus.FAILED.value in response.json()["detail"]
    assert "ProviderTransportError: builder unavailable" in response.json()["detail"]


async def test_create_host_rejects_unknown_template_id(client):
    """An unknown template ID is bad host-create input, not a route 404."""
    missing_id = str(uuid7())

    response = await client.post(
        "/hosts",
        headers=AUTH_HEADERS,
        json={"template": missing_id},
    )

    assert response.status_code == 400
    assert missing_id in response.json()["detail"]
    assert "not found" in response.json()["detail"]
    assert response.json()["error_code"] == "UNKNOWN_TEMPLATE"


async def test_create_host_explicit_image_wins_without_touching_template(client, monkeypatch):
    """An explicit image bypasses template resolution and leaves usage unstamped."""
    template = await create_template_record(image="derived:ignored")
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    response = await client.post(
        "/hosts",
        headers=AUTH_HEADERS,
        json={"image": "explicit:image", "template": str(template.id)},
    )

    assert response.status_code == 201
    assert response.json()["image"] == "explicit:image"
    async with async_session_factory() as session:
        untouched_template = await session.get(Template, template.id)
    assert untouched_template is not None
    assert untouched_template.last_used_at is None


async def test_create_host_template_request_bypasses_pool(client, monkeypatch):
    """A template request provisions fresh instead of claiming a default-image pool host."""
    monkeypatch.setenv("POOL_SIZE", "1")
    monkeypatch.delenv("POOL_SIZES", raising=False)
    get_settings.cache_clear()
    now = utc_now()
    pool_host = Host(
        id=uuid7(),
        name="sb-template-pool",
        status=HostStatus.ACTIVE.value,
        provider="exe",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        env={},
        internal_ssh_host="sb-template-pool.example.ts.net",
        external_ssh_host="",
        external_ssh_port=22,
        known_hosts="",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=4),
        pool_member=True,
        last_error="",
    )
    template = await create_template_record(image="derived:fresh")
    async with async_session_factory() as session:
        session.add(pool_host)
        await session.commit()
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    try:
        response = await client.post(
            "/hosts",
            headers=AUTH_HEADERS,
            json={"template": str(template.id)},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 201
    assert response.json()["id"] != str(pool_host.id)
    assert response.json()["image"] == "derived:fresh"
    mocked_provision.assert_awaited_once()


async def test_create_host_cannot_resolve_another_providers_template(client):
    """A template belonging to another provider is invisible to host creation."""
    template = await create_template_record(
        provider="other-provider",
        image="derived:other-provider",
    )

    response = await client.post(
        "/hosts",
        headers=AUTH_HEADERS,
        json={"template": str(template.id)},
    )

    assert response.status_code == 400
    assert str(template.id) in response.json()["detail"]
    assert "provider 'exe'" in response.json()["detail"]
