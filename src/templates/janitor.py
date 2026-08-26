import logging
from datetime import timedelta

from sqlalchemy import select, update

from core.database import async_session_factory
from core.exceptions import ResourceNotFoundError
from core.settings import get_settings
from hosts.service import utc_now
from templates.models import Template, TemplateStatus
from templates.service import TemplateService, reapable

logger = logging.getLogger(__name__)


async def reap_templates() -> None:
    settings = get_settings()

    # Abandoned builds become failed in one guarded UPDATE — the WHERE is
    # the race guard, like the pool claim. This is the only exit for a
    # build whose process died; the failed-retention sweep deletes it later.
    async with async_session_factory() as session:
        result = await session.execute(
            update(Template)
            .where(Template.status == TemplateStatus.BUILDING.value)
            .where(
                Template.updated_at < utc_now() - timedelta(seconds=settings.template_build_timeout)
            )
            .values(
                status=TemplateStatus.FAILED.value,
                last_error=(
                    "build abandoned: exceeded the "
                    f"{settings.template_build_timeout}-second build timeout"
                ),
                updated_at=utc_now(),
            )
            .returning(Template.id)
        )
        abandoned_ids = list(result.scalars())
        await session.commit()
    if abandoned_ids:
        logger.info("template janitor: marked %s abandoned build(s) failed", len(abandoned_ids))

    async with async_session_factory() as session:
        candidate_ids = list(
            (await session.execute(select(Template.id).where(reapable(settings)))).scalars()
        )

    for template_id in candidate_ids:
        try:
            async with async_session_factory() as session:
                deleted = await TemplateService(session).delete(template_id, expired_only=True)
        except ResourceNotFoundError:
            continue
        except Exception:
            logger.exception(
                "template janitor: failed to reap template: template_id=%s", template_id
            )
        else:
            if deleted:
                logger.info("template janitor: reaped template: template_id=%s", template_id)
