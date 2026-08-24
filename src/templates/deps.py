from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from templates.service import TemplateService


async def get_template_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TemplateService:
    return TemplateService(session)
