from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from core.database import Base
from core.settings import get_settings
from hosts import models  # noqa: F401
from templates import models as template_models  # noqa: F401

config = context.config

# Only apply alembic.ini logging when Alembic is the entry point (CLI).
# When imported by the app or tests, app.py's logging config takes precedence.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_database_url() -> str:
    # Alembic drives the engine synchronously. psycopg3 uses the same
    # postgresql+psycopg:// spec for sync and async engines. SQLite's
    # aiosqlite driver is async-only, so rewrite it to the stdlib sqlite3
    # driver here — same on-disk database, sync access path.
    url = get_settings().database_url
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _sync_database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
