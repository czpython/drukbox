import hashlib
import secrets

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class ServiceAccount(Base):
    __tablename__ = "service_accounts"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)

    def issue_token(self) -> str:
        token = f"drkb_{secrets.token_urlsafe(32)}"
        self.fingerprint = self.get_fingerprint(token)
        return token

    @classmethod
    async def authenticate(cls, session: AsyncSession, token: str) -> bool:
        return bool(
            await session.scalar(
                select(cls.name).where(cls.fingerprint == cls.get_fingerprint(token))
            )
        )

    @staticmethod
    def get_fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
