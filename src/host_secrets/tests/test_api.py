import uuid

from sqlalchemy import select, text

from core.database import async_session_factory
from hosts.models import Host
from hosts.service import utc_now

AUTH_HEADERS = {"Authorization": "Bearer service-token"}


async def test_registers_a_static_built_in_secret(client) -> None:
    host = await _create_host_record()

    response = await client.put(
        f"/hosts/{host.id}/secrets/github",
        headers=AUTH_HEADERS,
        json={"value": "github-static-secret"},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert await _stored_secrets(host.id) == {"github": {"value": "github-static-secret"}}


async def test_registers_a_refreshable_built_in_secret(client) -> None:
    host = await _create_host_record()
    source = {
        "url": "https://mint.example.test/boxes/box-1/github?audience=api",
        "headers": {"Authorization": "Bearer mint-credential"},
        "refresh": "50m",
    }

    response = await client.put(
        f"/hosts/{host.id}/secrets/github",
        headers=AUTH_HEADERS,
        json={"source": source},
    )

    assert response.status_code == 204
    assert await _stored_secrets(host.id) == {"github": {"source": source}}


async def test_registers_a_custom_secret_without_catalog_lookup(client) -> None:
    host = await _create_host_record()
    entry = {
        "host": "api.acme.test",
        "auth_var": "ACME_TOKEN",
        "placeholder": "acme-proxy-managed",
        "value": "acme-static-secret",
    }

    response = await client.put(
        f"/hosts/{host.id}/secrets/acme",
        headers=AUTH_HEADERS,
        json=entry,
    )

    assert response.status_code == 204
    assert await _stored_secrets(host.id) == {"acme": entry}


async def test_registers_a_refreshable_custom_secret(client) -> None:
    host = await _create_host_record()
    entry = {
        "host": "api.acme.test",
        "auth_var": "ACME_TOKEN",
        "placeholder": "acme-proxy-managed",
        "source": {
            "url": "https://mint.example.test/boxes/box-1/acme",
            "headers": {"Authorization": "Bearer mint-credential"},
            "refresh": "30m",
        },
    }

    response = await client.put(
        f"/hosts/{host.id}/secrets/acme",
        headers=AUTH_HEADERS,
        json=entry,
    )

    assert response.status_code == 204
    assert await _stored_secrets(host.id) == {"acme": entry}


async def test_register_replaces_one_service_without_changing_others(client) -> None:
    host = await _create_host_record(
        secrets={
            "github": {"value": "old-github-secret"},
            "openai": {"value": "openai-secret"},
        }
    )

    response = await client.put(
        f"/hosts/{host.id}/secrets/github",
        headers=AUTH_HEADERS,
        json={"value": "new-github-secret"},
    )

    assert response.status_code == 204
    assert await _stored_secrets(host.id) == {
        "github": {"value": "new-github-secret"},
        "openai": {"value": "openai-secret"},
    }


async def test_register_rejects_an_unknown_built_in_service(client) -> None:
    host = await _create_host_record()

    response = await client.put(
        f"/hosts/{host.id}/secrets/acme",
        headers=AUTH_HEADERS,
        json={"value": "secret"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "unknown secret service 'acme'",
        "error_code": "UNKNOWN_SECRET_SERVICE",
    }
    assert await _stored_secrets(host.id) == {}


async def test_register_rejects_missing_host(client) -> None:
    host_id = uuid.UUID("00000000-0000-0000-0000-000000000151")

    response = await client.put(
        f"/hosts/{host_id}/secrets/github",
        headers=AUTH_HEADERS,
        json={"value": "secret"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "host not found"


async def test_register_requires_service_auth(client) -> None:
    host = await _create_host_record()

    missing = await client.put(
        f"/hosts/{host.id}/secrets/github",
        json={"value": "secret"},
    )
    rejected = await client.put(
        f"/hosts/{host.id}/secrets/github",
        headers={"Authorization": "Bearer wrong-token"},
        json={"value": "secret"},
    )

    assert missing.status_code == 401
    assert rejected.status_code == 403


async def test_validation_response_does_not_echo_secret_material(client) -> None:
    host = await _create_host_record()
    payload = {
        "value": "static-secret-that-must-not-leak",
        "source": {
            "url": "https://mint.example.test/token",
            "headers": {"Authorization": "Bearer fetch-secret-that-must-not-leak"},
            "refresh": "50m",
        },
    }

    response = await client.put(
        f"/hosts/{host.id}/secrets/github",
        headers=AUTH_HEADERS,
        json=payload,
    )

    assert response.status_code == 422
    assert "static-secret-that-must-not-leak" not in response.text
    assert "fetch-secret-that-must-not-leak" not in response.text
    assert all("input" not in error for error in response.json()["detail"])


async def test_register_rejects_an_invalid_service_name(client) -> None:
    host = await _create_host_record()

    response = await client.put(
        f"/hosts/{host.id}/secrets/GitHub",
        headers=AUTH_HEADERS,
        json={"value": "secret"},
    )

    assert response.status_code == 422
    assert await _stored_secrets(host.id) == {}


async def test_registered_secret_is_ciphertext_at_rest(client) -> None:
    host = await _create_host_record()

    response = await client.put(
        f"/hosts/{host.id}/secrets/github",
        headers=AUTH_HEADERS,
        json={"value": "registered-secret-at-rest"},
    )

    assert response.status_code == 204
    async with async_session_factory() as session:
        stored = (await session.execute(text("SELECT secrets FROM hosts"))).scalar_one()
    assert b"registered-secret-at-rest" not in bytes(stored)


async def _create_host_record(*, secrets: dict[str, object] | None = None) -> Host:
    now = utc_now()
    host = Host(
        name=f"sb-{uuid.uuid4().hex[:12]}",
        image="sandbox:latest",
        secrets=secrets or {},
        created_at=now,
        updated_at=now,
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()
    return host


async def _stored_secrets(host_id: uuid.UUID) -> dict[str, object]:
    async with async_session_factory() as session:
        host = (await session.execute(select(Host).where(Host.id == host_id))).scalar_one()
        return dict(host.secrets)
