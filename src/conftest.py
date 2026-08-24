import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

TEST_ENV_PATH = Path(__file__).resolve().parents[1] / "env" / "test.env"

# Tests always run against a disposable database and drop/recreate every table.
# We never honor an ambient DATABASE_URL — it might be a real one — so it's set
# unconditionally from TEST_DATABASE_URL (CI overrides this for the Postgres
# matrix), defaulting to a local SQLite file. This must run before anything
# imports core.database, which builds the engine from DATABASE_URL.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "sqlite+aiosqlite:///./.drukbox-test.db"
)

# The suite hard-codes "service-token" as the valid bearer; set it authoritatively
# so an ambient SERVICE_TOKENS (a dev shell, a CI job) can't shadow it through the
# loader below and 403 every auth test.
os.environ["SERVICE_TOKENS"] = "service-token"


def load_test_env() -> None:
    # Assign (not setdefault) so an ambient shell value can't shadow a test default.
    for raw_line in TEST_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        key, value = line.split("=", 1)
        os.environ[key] = value


load_test_env()


@pytest.fixture(autouse=True)
async def reset_database() -> AsyncGenerator[None]:
    from core.database import Base, engine
    from hosts import models  # noqa: F401
    from templates import models as template_models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    yield


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    from api.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="https://drukbox.example",
    ) as client:
        yield client
