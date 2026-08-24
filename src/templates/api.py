import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy.exc import SQLAlchemyError

from hosts.auth import require_service_auth
from providers.exceptions import ProviderError, UnknownProviderError
from templates.deps import get_template_service
from templates.exceptions import TemplateTeardownError
from templates.models import Template
from templates.schemas import TemplateCreate, TemplateOut
from templates.service import TemplateService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["templates"])

TemplateServiceDep = Annotated[TemplateService, Depends(get_template_service)]


@router.post(
    "",
    response_model=TemplateOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_auth)],
)
async def create_template(
    payload: TemplateCreate,
    background_tasks: BackgroundTasks,
    service: TemplateServiceDep,
) -> Template:
    try:
        template, created = await service.get_or_create(
            provider=payload.provider,
            base_image=payload.base_image,
            setup_script=payload.setup_script,
            label=payload.label,
        )
    except UnknownProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        logger.exception("unexpected database error during template creation")
        raise HTTPException(
            status_code=503,
            detail="template creation could not be completed",
        ) from exc

    if created:
        background_tasks.add_task(service.build, template.id)
    return template


@router.get(
    "",
    response_model=list[TemplateOut],
    dependencies=[Depends(require_service_auth)],
)
async def list_templates(service: TemplateServiceDep) -> list[Template]:
    return await service.list()


@router.get(
    "/{template_id}",
    response_model=TemplateOut,
    dependencies=[Depends(require_service_auth)],
)
async def get_template(template_id: uuid.UUID, service: TemplateServiceDep) -> Template:
    if template := await service.get(template_id):
        return template
    raise HTTPException(status_code=404, detail="template not found")


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_service_auth)],
)
async def delete_template(template_id: uuid.UUID, service: TemplateServiceDep) -> Response:
    try:
        await service.delete(template_id)
    except ProviderError as exc:
        logger.exception("unexpected error deleting template")
        raise TemplateTeardownError("template teardown could not be completed") from exc
    except SQLAlchemyError as exc:
        logger.exception("unexpected database error during template teardown")
        raise TemplateTeardownError("template teardown could not be completed") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
