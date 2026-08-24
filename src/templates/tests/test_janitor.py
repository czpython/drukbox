import hashlib
from datetime import timedelta
from unittest.mock import AsyncMock

from uuid6 import uuid7

from core.database import async_session_factory
from core.settings import get_settings
from hosts.service import utc_now
from providers.exceptions import ProviderNotFoundError, ProviderTransportError
from templates.janitor import reap_templates
from templates.models import Template, TemplateStatus
from templates.service import TemplateService


async def _create_template(
    *,
    provider: str,
    status: TemplateStatus,
    age: timedelta,
    name: str,
    handle: str = "",
    last_used_age: timedelta | None = None,
) -> Template:
    now = utc_now()
    setup_script = f"echo {name}"
    template = Template(
        id=uuid7(),
        provider=provider,
        base_image="stub:base",
        requirements_hash=hashlib.sha256(setup_script.encode()).hexdigest(),
        setup_script=setup_script,
        label=name,
        handle=handle,
        status=status.value,
        last_error="",
        created_at=now - age,
        updated_at=now - age,
        last_used_at=now - last_used_age if last_used_age is not None else None,
    )
    async with async_session_factory() as session:
        session.add(template)
        await session.commit()
        await session.refresh(template)
    return template


async def test_janitor_marks_only_abandoned_builds_failed(template_provider):
    settings = get_settings()
    abandoned = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.BUILDING,
        age=timedelta(minutes=settings.template_build_timeout_minutes + 1),
        name="abandoned",
    )
    fresh = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.BUILDING,
        age=timedelta(minutes=settings.template_build_timeout_minutes - 1),
        name="fresh",
    )

    await reap_templates()

    async with async_session_factory() as session:
        abandoned = await session.get(Template, abandoned.id)
        fresh = await session.get(Template, fresh.id)

    assert abandoned
    assert abandoned.status == TemplateStatus.FAILED.value
    assert abandoned.last_error == (
        f"build abandoned: exceeded {settings.template_build_timeout_minutes}-minute timeout"
    )
    assert fresh
    assert fresh.status == TemplateStatus.BUILDING.value
    assert fresh.last_error == ""


async def test_janitor_reaps_only_failed_templates_past_retention(template_provider):
    settings = get_settings()
    expired = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.FAILED,
        age=timedelta(hours=settings.template_failed_retention_hours + 1),
        name="expired-failure",
    )
    recent = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.FAILED,
        age=timedelta(hours=settings.template_failed_retention_hours - 1),
        name="recent-failure",
    )

    await reap_templates()

    async with async_session_factory() as session:
        assert await session.get(Template, expired.id) is None
        assert await session.get(Template, recent.id) is not None


async def test_janitor_reaps_unused_templates_by_last_use(template_provider):
    settings = get_settings()
    expired_age = timedelta(days=settings.template_unused_ttl_days + 1)
    fresh_age = timedelta(days=settings.template_unused_ttl_days - 1)
    never_used = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE,
        age=expired_age,
        name="never-used",
        handle="stub-template:never-used",
    )
    used_long_ago = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE,
        age=expired_age + timedelta(days=1),
        name="used-long-ago",
        handle="stub-template:used-long-ago",
        last_used_age=expired_age,
    )
    recently_used = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE,
        age=expired_age + timedelta(days=1),
        name="recently-used",
        handle="stub-template:recently-used",
        last_used_age=fresh_age,
    )

    await reap_templates()

    assert set(template_provider.deleted) == {
        "stub-template:used-long-ago",
        "stub-template:never-used",
    }
    async with async_session_factory() as session:
        assert await session.get(Template, never_used.id) is None
        assert await session.get(Template, used_long_ago.id) is None
        assert await session.get(Template, recently_used.id) is not None


async def test_delete_spares_template_used_after_candidate_selection(template_provider):
    settings = get_settings()
    reap_before = utc_now() - timedelta(days=settings.template_unused_ttl_days)
    template = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE,
        age=timedelta(days=settings.template_unused_ttl_days + 1),
        name="leased-in-race",
        handle="stub-template:leased-in-race",
    )

    async with async_session_factory() as session:
        leased = await session.get(Template, template.id)
        assert leased
        leased.last_used_at = utc_now()
        await session.commit()

    async with async_session_factory() as session:
        deleted = await TemplateService(session).delete(
            template.id,
            reap_status=TemplateStatus.AVAILABLE,
            reap_before=reap_before,
        )

    assert not deleted
    assert template_provider.deleted == []
    async with async_session_factory() as session:
        assert await session.get(Template, template.id) is not None


async def test_janitor_removes_row_when_provider_artifact_is_already_gone(template_provider):
    settings = get_settings()
    template_provider.delete_error = ProviderNotFoundError("already gone")
    template = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE,
        age=timedelta(days=settings.template_unused_ttl_days + 1),
        name="missing-artifact",
        handle="stub-template:missing-artifact",
    )

    await reap_templates()

    async with async_session_factory() as session:
        assert await session.get(Template, template.id) is None


async def test_janitor_keeps_transport_failure_and_continues(template_provider, monkeypatch):
    settings = get_settings()
    delete_template = AsyncMock(side_effect=[ProviderTransportError("offline"), None])
    monkeypatch.setattr(template_provider, "delete_template", delete_template)
    failed_delete = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE,
        age=timedelta(days=settings.template_unused_ttl_days + 2),
        name="failed-delete",
        handle="stub-template:failed-delete",
    )
    next_candidate = await _create_template(
        provider=template_provider.name,
        status=TemplateStatus.AVAILABLE,
        age=timedelta(days=settings.template_unused_ttl_days + 1),
        name="next-candidate",
        handle="stub-template:next-candidate",
    )

    await reap_templates()

    assert delete_template.await_args_list[0].args == ("stub-template:failed-delete",)
    assert delete_template.await_args_list[1].args == ("stub-template:next-candidate",)
    async with async_session_factory() as session:
        assert await session.get(Template, failed_delete.id) is not None
        assert await session.get(Template, next_candidate.id) is None
