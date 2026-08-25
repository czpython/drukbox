import logging
from datetime import timedelta

from sqlalchemy import func, select

from core.database import async_session_factory
from core.exceptions import ResourceNotFoundError
from core.settings import get_settings
from hosts.service import utc_now
from templates.models import Template, TemplateStatus
from templates.service import TemplateService

logger = logging.getLogger(__name__)


async def reap_templates() -> None:
    settings = get_settings()
    now = utc_now()
    build_cutoff = now - timedelta(minutes=settings.template_build_timeout_minutes)
    failed_cutoff = now - timedelta(hours=settings.template_failed_retention_hours)
    unused_cutoff = now - timedelta(days=settings.template_unused_ttl_days)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Template.id)
            .where(Template.status == TemplateStatus.BUILDING.value)
            .where(Template.updated_at < build_cutoff)
            .order_by(Template.updated_at, Template.id)
        )
        abandoned_ids = list(result.scalars())

    for template_id in abandoned_ids:
        try:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Template).where(Template.id == template_id).with_for_update()
                )
                template = result.scalar_one_or_none()
                if not template:
                    continue
                if (
                    template.status != TemplateStatus.BUILDING.value
                    or template.updated_at >= build_cutoff
                ):
                    continue

                template.status = TemplateStatus.FAILED.value
                template.last_error = (
                    "build abandoned: exceeded "
                    f"{settings.template_build_timeout_minutes}-minute timeout"
                )
                template.updated_at = utc_now()
                await session.commit()
        except Exception:
            logger.exception(
                "template janitor: failed to mark abandoned build: template_id=%s",
                template_id,
            )
        else:
            logger.info(
                "template janitor: marked abandoned build failed: template_id=%s",
                template_id,
            )

    last_used_at = func.coalesce(Template.last_used_at, Template.created_at)
    reap_sweeps = (
        (
            TemplateStatus.FAILED,
            failed_cutoff,
            "failed",
            select(Template.id)
            .where(Template.status == TemplateStatus.FAILED.value)
            .where(Template.updated_at < failed_cutoff)
            .order_by(Template.updated_at, Template.id),
        ),
        (
            TemplateStatus.AVAILABLE,
            unused_cutoff,
            "unused",
            select(Template.id)
            .where(Template.status == TemplateStatus.AVAILABLE.value)
            .where(last_used_at < unused_cutoff)
            .order_by(last_used_at, Template.id),
        ),
    )

    for reap_status, reap_before, reap_reason, candidate_query in reap_sweeps:
        async with async_session_factory() as session:
            result = await session.execute(candidate_query)
            candidate_ids = list(result.scalars())

        for template_id in candidate_ids:
            try:
                async with async_session_factory() as session:
                    deleted = await TemplateService(session).delete(
                        template_id,
                        reap_status=reap_status,
                        reap_before=reap_before,
                    )
            except ResourceNotFoundError:
                continue
            except Exception:
                logger.exception(
                    "template janitor: failed to reap %s template: template_id=%s",
                    reap_reason,
                    template_id,
                )
            else:
                if deleted:
                    logger.info(
                        "template janitor: reaped %s template: template_id=%s",
                        reap_reason,
                        template_id,
                    )
