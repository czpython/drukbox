import hashlib
import secrets
from typing import ClassVar

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from service_accounts.exceptions import ServiceAccountTokenRejectedError


class ServiceAccount(Base):
    __tablename__ = "service_accounts"

    # The admin account is seeded by migration. It has no token: admin keys
    # come from SERVICE_TOKENS. Its row reserves the name.
    ADMIN: ClassVar[str] = "admin"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True)

    def issue_token(self) -> str:
        token = f"drkb_{secrets.token_urlsafe(32)}"
        self.fingerprint = self.get_fingerprint(token)
        return token

    @classmethod
    async def authenticate(cls, session: AsyncSession, token: str) -> str:
        if name := await session.scalar(
            select(cls.name).where(cls.fingerprint == cls.get_fingerprint(token))
        ):
            return name
        raise ServiceAccountTokenRejectedError("service account token rejected")

    @staticmethod
    def get_fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
