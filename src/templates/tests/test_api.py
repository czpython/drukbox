import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from uuid6 import uuid7

from core.database import async_session_factory
from providers import registry as registry_module
from providers.base import VMProvider
from providers.docker.derived_image import derive_image_tag
from providers.exceptions import ProviderNotFoundError, ProviderTransportError
from templates.exceptions import TemplateStateError
from templates.models import Template, TemplateStatus
from templates.service import TemplateService

AUTH_HEADERS = {"Authorization": "Bearer service-token"}
SETUP_SCRIPT = "apt-get update && apt-get install -y nodejs"


async def test_create_template_returns_building_then_becomes_available(client, template_provider):
    """Create returns the pollable building record before the background build result."""
    response = await client.post(
        "/templates",
        headers=AUTH_HEADERS,
        json={
            "provider": template_provider.name,
            "setup_script": SETUP_SCRIPT,
            "label": "Node tools",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert uuid.UUID(payload["id"]).version == 7
    assert payload["provider"] == template_provider.name
    assert payload["base_image"] == template_provider.default_image
    assert payload["setup_script_hash"] == hashlib.sha256(SETUP_SCRIPT.encode()).hexdigest()
    assert payload["label"] == "Node tools"
    assert payload["image"] == ""
    assert payload["status"] == TemplateStatus.BUILDING.value
    assert payload["last_error"] == ""
    assert payload["last_used_at"] is None
    assert "setup_script" not in payload

    polled = await client.get(f"/templates/{payload['id']}", headers=AUTH_HEADERS)

    assert polled.status_code == 200
    assert polled.json()["status"] == TemplateStatus.AVAILABLE.value
    assert polled.json()["image"] == derive_image_tag(
        base_image=template_provider.default_image,
        setup_script=SETUP_SCRIPT,
    )
    assert "setup_script" not in polled.json()
    assert template_provider.built == [
        (template_provider.default_image, SETUP_SCRIPT, "Node tools")
    ]


async def test_unexpected_build_crash_is_pollable(client, template_provider):
    """Unexpected strategy crashes persist as failed template diagnostics."""
    template_provider.build_error = OSError("builder crashed")

    response = await client.post(
        "/templates",
        headers=AUTH_HEADERS,
        json={"provider": template_provider.name, "setup_script": SETUP_SCRIPT},
    )
    polled = await client.get(f"/templates/{response.json()['id']}", headers=AUTH_HEADERS)

    assert response.status_code == 202
    assert polled.json()["status"] == TemplateStatus.FAILED.value
    assert polled.json()["last_error"] == "OSError: builder crashed"


async def test_unsupported_capability_becomes_failed_build(client, monkeypatch):
    """A provider without template support reports failure through polling."""
    provider = MagicMock(spec=VMProvider)
    provider.name = "without-templates"
    provider.default_image = "stub:base"
    monkeypatch.setitem(registry_module._factories, provider.name, lambda: provider)
    monkeypatch.setitem(registry_module._instances, provider.name, provider)

    response = await client.post(
        "/templates",
        headers=AUTH_HEADERS,
        json={"provider": provider.name, "setup_script": SETUP_SCRIPT},
    )
    polled = await client.get(f"/templates/{response.json()['id']}", headers=AUTH_HEADERS)

    assert response.status_code == 202
    assert polled.json()["status"] == TemplateStatus.FAILED.value
    assert polled.json()["last_error"].startswith("CapabilityUnsupportedError:")
    assert "TemplateCapability" in polled.json()["last_error"]


async def test_duplicate_create_returns_existing_without_rebuilding(client, template_provider):
    """The same provider, base, and script reuse one record and one build."""
    first = await client.post(
        "/templates",
        headers=AUTH_HEADERS,
        json={
            "provider": template_provider.name,
            "base_image": "stub:custom",
            "setup_script": SETUP_SCRIPT,
            "label": "first label",
        },
    )
    second = await client.post(
        "/templates",
        headers=AUTH_HEADERS,
        json={
            "provider": template_provider.name,
            "base_image": "stub:custom",
            "setup_script": SETUP_SCRIPT,
            "label": "ignored label",
        },
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["status"] == TemplateStatus.AVAILABLE.value
    assert second.json()["label"] == "first label"
    assert template_provider.built == [("stub:custom", SETUP_SCRIPT, "first label")]


async def test_concurrent_creates_resolve_unique_index_race(template_provider):
    """Concurrent identical inserts converge on the unique-index winner."""
    async with (
        async_session_factory() as first_session,
        async_session_factory() as second_session,
    ):
        first_service = TemplateService(first_session)
        second_service = TemplateService(second_session)
        results = await asyncio.gather(
            first_service.get_or_create(
                provider=template_provider.name,
                base_image="stub:race",
                setup_script=SETUP_SCRIPT,
                label="first",
            ),
            second_service.get_or_create(
                provider=template_provider.name,
                base_image="stub:race",
                setup_script=SETUP_SCRIPT,
                label="second",
            ),
        )

    assert results[0][0].id == results[1][0].id
    assert sorted(created for _, created in results) == [False, True]

    async with async_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Template))
    assert count == 1


async def test_create_race_without_winner_returns_typed_conflict(monkeypatch, template_provider):
    """A vanished unique-index winner returns a retryable template state conflict."""
    create_session = AsyncMock()
    create_session.add = MagicMock()
    create_session.commit.side_effect = IntegrityError("insert", {}, Exception("unique"))
    create_context = MagicMock()
    create_context.__aenter__ = AsyncMock(return_value=create_session)
    create_context.__aexit__ = AsyncMock(return_value=False)
    create_session_factory = MagicMock(return_value=create_context)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    request_session = AsyncMock()
    request_session.execute.return_value = result
    monkeypatch.setattr("templates.service.async_session_factory", create_session_factory)

    with pytest.raises(
        TemplateStateError,
        match="template creation race could not be resolved",
    ):
        await TemplateService(request_session).get_or_create(
            provider=template_provider.name,
            base_image="stub:race",
            setup_script=SETUP_SCRIPT,
            label="race",
        )

    create_session.rollback.assert_awaited_once()


async def test_get_template_returns_not_found(client):
    """A missing template returns the resource-specific 404 detail."""
    template_id = uuid.UUID("00000000-0000-0000-0000-000000000877")

    response = await client.get(f"/templates/{template_id}", headers=AUTH_HEADERS)

    assert response.status_code == 404
    assert response.json()["detail"] == "template not found"


async def test_list_templates_returns_newest_first(client, template_provider):
    """List orders template records from newest to oldest without setup scripts."""
    now = datetime.now(UTC)
    older = await create_template_record(
        provider=template_provider.name,
        status=TemplateStatus.FAILED.value,
        created_at=now,
        label="older",
    )
    newer = await create_template_record(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE.value,
        created_at=now + timedelta(seconds=1),
        base_image="stub:newer",
        label="newer",
        image="stub-template:newer",
    )

    response = await client.get("/templates", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [str(newer.id), str(older.id)]
    assert all("setup_script" not in item for item in response.json())


async def test_delete_building_template_returns_conflict(client, template_provider):
    """Deletion refuses a template whose provider build may still be running."""
    template = await create_template_record(
        provider=template_provider.name,
        status=TemplateStatus.BUILDING.value,
    )

    response = await client.delete(f"/templates/{template.id}", headers=AUTH_HEADERS)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "template is still building",
        "error_code": "TEMPLATE_STATE",
    }
    assert template_provider.deleted == []


async def test_delete_available_template_removes_provider_artifact(client, template_provider):
    """Deleting an available template removes its artifact and database row."""
    template = await create_template_record(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE.value,
        image="stub-template:available",
    )

    response = await client.delete(f"/templates/{template.id}", headers=AUTH_HEADERS)

    assert response.status_code == 204
    assert response.content == b""
    assert template_provider.deleted == ["stub-template:available"]
    async with async_session_factory() as session:
        assert await session.get(Template, template.id) is None


async def test_delete_tolerates_missing_provider_artifact(client, template_provider):
    """An already-absent provider artifact does not strand the template row."""
    template_provider.delete_error = ProviderNotFoundError("already gone")
    template = await create_template_record(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE.value,
        image="stub-template:missing",
    )

    response = await client.delete(f"/templates/{template.id}", headers=AUTH_HEADERS)

    assert response.status_code == 204
    async with async_session_factory() as session:
        assert await session.get(Template, template.id) is None


async def test_delete_provider_failure_preserves_record(client, template_provider):
    """A provider teardown failure returns 503 and leaves the row retryable."""
    template_provider.delete_error = ProviderTransportError("provider unavailable")
    template = await create_template_record(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE.value,
        image="stub-template:retry",
    )

    response = await client.delete(f"/templates/{template.id}", headers=AUTH_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == "template teardown could not be completed"
    async with async_session_factory() as session:
        assert await session.get(Template, template.id) is not None


async def test_create_template_rejects_unknown_provider(client):
    """An unregistered provider returns 400 with the available-provider context."""
    response = await client.post(
        "/templates",
        headers=AUTH_HEADERS,
        json={"provider": "does-not-exist", "setup_script": SETUP_SCRIPT},
    )

    assert response.status_code == 400
    assert "does-not-exist" in response.json()["detail"]
    assert "available" in response.json()["detail"]


async def test_create_template_rejects_blank_script(client, template_provider):
    """Whitespace-only setup scripts stop at the wire boundary."""
    response = await client.post(
        "/templates",
        headers=AUTH_HEADERS,
        json={"provider": template_provider.name, "setup_script": "  \n"},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "setup_script"]
    assert template_provider.built == []


async def create_template_record(
    *,
    provider: str,
    status: str,
    created_at: datetime | None = None,
    base_image: str = "stub:base",
    label: str = "",
    image: str = "",
) -> Template:
    now = created_at or datetime.now(UTC)
    template = Template(
        id=uuid7(),
        provider=provider,
        base_image=base_image,
        setup_script_hash=hashlib.sha256(SETUP_SCRIPT.encode()).hexdigest(),
        setup_script=SETUP_SCRIPT,
        label=label,
        image=image,
        status=status,
        last_error="",
        created_at=now,
        updated_at=now,
    )
    async with async_session_factory() as session:
        session.add(template)
        await session.commit()
        await session.refresh(template)
    return template
