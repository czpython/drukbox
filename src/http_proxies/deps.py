from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from http_proxies.service import HTTPProxyService


async def get_http_proxy_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTTPProxyService:
    return HTTPProxyService(session)
