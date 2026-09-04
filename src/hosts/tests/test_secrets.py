from sqlalchemy import select, text

from core.database import async_session_factory
from hosts.models import Host
from hosts.service import utc_now


async def test_host_secrets_are_ciphertext_at_rest_and_decrypt_on_read() -> None:
    secrets = {
        "source": {
            "url": "https://mint.example/token",
            "headers": {"Authorization": "Bearer fetch-token"},
        },
        "static": "static-token",
    }
    now = utc_now()

    async with async_session_factory() as session:
        session.add(
            Host(
                name="sb-encrypted",
                image="sandbox:latest",
                env={"VISIBLE_SETTING": "ordinary-value"},
                secrets=secrets,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with async_session_factory() as session:
        stored = (await session.execute(text("SELECT env, secrets FROM hosts"))).one()
        loaded = (await session.execute(select(Host))).scalar_one()

    ciphertext = bytes(stored.secrets)
    assert b"fetch-token" not in ciphertext
    assert b"static-token" not in ciphertext
    assert "ordinary-value" in str(stored.env)
    assert dict(loaded.secrets) == secrets
    assert repr(loaded.secrets) == "SecretsMapping(<redacted>)"
