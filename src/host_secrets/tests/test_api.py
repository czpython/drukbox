import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from core.database import async_session_factory
from core.settings import get_settings
from hosts.models import Host

AUTH_HEADERS = {"Authorization": "Bearer service-token"}


async def test_a_host_is_created_with_its_secrets_and_never_returns_them(
    client, monkeypatch
) -> None:
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())
    secrets = {
        "anthropic": {"value": "sk-ant-real"},
        "github": {
            "host": "api.github.com",
            "credential_var": "GH_TOKEN",
            "endpoint_var": "GH_API_URL",
            "issuer": {
                "url": "https://mint.example.test/boxes/box-1/github",
                "headers": {"Authorization": "Bearer mint-credential"},
                "refresh": "50m",
            },
        },
    }

    response = await client.post("/hosts", headers=AUTH_HEADERS, json={"secrets": secrets})

    assert response.status_code == 201
    assert "secrets" not in response.json()
    assert "sk-ant-real" not in response.text
    assert await _stored_secrets(uuid.UUID(response.json()["id"])) == {
        "anthropic": {"value": "sk-ant-real"},
        "github": {
            **secrets["github"],
            "credential_header": "Authorization",
            "credential_prefix": "Bearer ",
            "base_path": "",
        },
    }


async def test_secrets_are_ciphertext_at_rest(client, monkeypatch) -> None:
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    await client.post(
        "/hosts", headers=AUTH_HEADERS, json={"secrets": {"anthropic": {"value": "sk-ant-real"}}}
    )

    async with async_session_factory() as session:
        stored = (await session.execute(text("SELECT secrets FROM hosts"))).scalar_one()
    assert b"sk-ant-real" not in bytes(stored)


@pytest.mark.parametrize(
    ("secrets", "reason"),
    [
        ({"acme": {"value": "one"}}, "unknown secret service"),
        ({"github": {"value": "one"}}, "no base URL variable"),
        ({"GitHub": {"value": "one"}}, "invalid secret service name"),
        (
            {
                "anthropic": {
                    "value": "one",
                    "issuer": {"url": "https://m.test", "headers": {"A": "b"}, "refresh": "5m"},
                }
            },
            "exactly one of value or issuer",
        ),
    ],
)
async def test_a_secret_the_exchange_cannot_serve_is_refused(
    client, secrets: dict[str, object], reason: str
) -> None:
    response = await client.post("/hosts", headers=AUTH_HEADERS, json={"secrets": secrets})

    assert response.status_code == 422
    assert reason in response.text
    assert "one" not in response.text.replace("exactly one", "")


async def test_secrets_need_the_exchange_address(client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "secrets_exchange_url", "")

    response = await client.post(
        "/hosts", headers=AUTH_HEADERS, json={"secrets": {"anthropic": {"value": "sk-ant-real"}}}
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "SECRETS_EXCHANGE_NOT_CONFIGURED"


async def _stored_secrets(host_id: uuid.UUID) -> dict[str, object]:
    async with async_session_factory() as session:
        host = (await session.execute(select(Host).where(Host.id == host_id))).scalar_one()
        return dict(host.secrets)
