import hmac
import logging
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.settings import get_settings
from service_accounts.models import ServiceAccount

logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


def is_admin_key(token: str) -> bool:
    # Bytes, not str: compare_digest raises TypeError on a non-ASCII str,
    # which would turn a bad bearer into a 500.
    settings = get_settings()
    return any(
        hmac.compare_digest(token.encode(), expected.encode())
        for expected in settings.service_tokens
    )


async def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str | None:
    if not credentials:
        raise HTTPException(status_code=401, detail="admin key or service account token required")
    if is_admin_key(credentials.credentials):
        return ServiceAccount.ADMIN

    try:
        return await ServiceAccount.authenticate(session, credentials.credentials)
    except SQLAlchemyError:
        logger.exception("service account token lookup failed")
        raise HTTPException(status_code=503, detail="service account tokens unavailable") from None


def require_admin_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    if not credentials:
        raise HTTPException(status_code=401, detail="admin key required")
    if not is_admin_key(credentials.credentials):
        raise HTTPException(status_code=403, detail="admin key rejected")
