import hashlib
import logging
import uuid
from datetime import timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import async_session_factory
from core.exceptions import ResourceNotFoundError
from core.settings import Settings, get_settings
from hosts.service import utc_now
from providers.capabilities import TemplateCapability, resolve_capability
from providers.exceptions import ProviderNotFoundError, UnknownProviderError
from providers.registry import get_provider_names, get_vm_provider
from templates.exceptions import TemplateStateError
from templates.models import Template, TemplateStatus

logger = logging.getLogger(__name__)


def reapable(settings: Settings) -> ColumnElement[bool]:
    """Templates the janitor may delete: failed past the retention window,
    or available and unleased past the unused TTL."""
    now = utc_now()
    last_active_at = func.coalesce(Template.last_used_at, Template.created_at)
    return or_(
        and_(
            Template.status == TemplateStatus.FAILED.value,
            Template.updated_at < now - timedelta(seconds=settings.template_failed_retention),
        ),
        and_(
            Template.status == TemplateStatus.AVAILABLE.value,
            last_active_at < now - timedelta(seconds=settings.template_unused_ttl),
        ),
    )


class TemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(
        self,
        *,
        provider: str | None,
        base_image: str | None,
        setup_script: str,
        label: str,
    ) -> tuple[Template, bool]:
        if provider:
            registered = get_provider_names()
            if provider not in registered:
                available = ", ".join(sorted(registered))
                raise UnknownProviderError(f"unknown provider {provider!r}; available: {available}")

        vm = get_vm_provider(provider)
        resolved_base_image = base_image or vm.default_image
        setup_script_hash = hashlib.sha256(setup_script.encode("utf-8")).hexdigest()
        now = utc_now()
        template = Template(
            provider=vm.name,
            base_image=resolved_base_image,
            setup_script_hash=setup_script_hash,
            setup_script=setup_script,
            label=label,
            image="",
            status=TemplateStatus.BUILDING.value,
            last_error="",
            created_at=now,
            updated_at=now,
        )

        async with async_session_factory() as create_session:
            create_session.add(template)
            try:
                await create_session.commit()
            except IntegrityError:
                await create_session.rollback()
            else:
                await create_session.refresh(template)
                return template, True

        winner = (
            await self.session.execute(
                select(Template)
                .where(Template.provider == vm.name)
                .where(Template.base_image == resolved_base_image)
                .where(Template.setup_script_hash == setup_script_hash)
            )
        ).scalar_one_or_none()
        if not winner:
            raise TemplateStateError("template creation race could not be resolved")
        return winner, False

    async def build(self, template_id: uuid.UUID) -> None:
        async with async_session_factory() as session:
            template = await session.get(Template, template_id)
            if not template:
                raise ResourceNotFoundError("template not found")
            # Release the read transaction before a minutes-long provider build.
            await session.commit()

            try:
                capability = resolve_capability(
                    get_vm_provider(template.provider),
                    TemplateCapability,
                )
                image = await capability.build_template_image(
                    base_image=template.base_image,
                    setup_script=template.setup_script,
                    label=template.label,
                )
            except Exception as exc:
                logger.exception(
                    "template build failed: template_id=%s provider=%s",
                    template.id,
                    template.provider,
                )
                template.status = TemplateStatus.FAILED.value
                template.last_error = f"{type(exc).__name__}: {exc}"
            else:
                template.image = image
                template.status = TemplateStatus.AVAILABLE.value
                template.last_error = ""

            template.updated_at = utc_now()
            await session.commit()

    async def get(self, template_id: uuid.UUID) -> Template | None:
        return await self.session.get(Template, template_id)

    async def list(self) -> list[Template]:
        result = await self.session.execute(select(Template).order_by(Template.created_at.desc()))
        return list(result.scalars())

    async def delete(self, template_id: uuid.UUID, *, expired_only: bool = False) -> bool:
        """Delete the template. Return False when the expired_only guard spares it."""
        result = await self.session.execute(
            select(Template).where(Template.id == template_id).with_for_update()
        )
        template = result.scalar_one_or_none()

        if not template:
            raise ResourceNotFoundError("template not found")

        if expired_only:
            still_reapable = (
                await self.session.execute(
                    select(Template.id)
                    .where(Template.id == template_id)
                    .where(reapable(get_settings()))
                )
            ).scalar_one_or_none()
            # A lease or status change since the janitor selected this row
            # revived it — spare it.
            if not still_reapable:
                return False

        if template.status == TemplateStatus.BUILDING.value:
            raise TemplateStateError("template is still building")

        if template.image:
            capability = resolve_capability(
                get_vm_provider(template.provider),
                TemplateCapability,
            )
            try:
                await capability.delete_template_image(template.image)
            except ProviderNotFoundError:
                logger.warning(
                    "template already absent at provider during teardown: "
                    "template_id=%s image=%s provider=%s",
                    template.id,
                    template.image,
                    template.provider,
                )

        await self.session.delete(template)
        await self.session.commit()
        return True
