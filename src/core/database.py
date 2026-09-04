from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy_encrypted_field import configure

from core.settings import get_settings


class Base(DeclarativeBase):
    pass


configure(lambda: get_settings().secrets_key.get_secret_value())

engine: AsyncEngine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with async_session_factory() as session:
        yield session


async def close_database() -> None:
    await engine.dispose()
