from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from host_secrets.service import HostSecretService


async def get_host_secret_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HostSecretService:
    return HostSecretService(session)
