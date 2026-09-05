import uuid
from unittest.mock import AsyncMock

from sqlalchemy import select, text

from core.database import async_session_factory
from host_secrets.placeholder import Placeholder
from hosts.models import Host, HostStatus
from hosts.service import utc_now
from providers.exceptions import ProviderCommandError

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

    response = await client.put(
        f"/hosts/{host.id}/secrets/acme",
        headers=AUTH_HEADERS,
        json={"host": "api.acme.test", "credential_var": "ACME_TOKEN", "value": "acme-secret"},
    )

    assert response.status_code == 204
    assert await _stored_secrets(host.id) == {
        "acme": {
            "host": "api.acme.test",
            "credential_header": "Authorization",
            "credential_prefix": "Bearer ",
            "credential_var": "ACME_TOKEN",
            "endpoint_var": "",
            "base_path": "",
            "value": "acme-secret",
        }
    }


async def test_registers_a_refreshable_custom_secret(client) -> None:
    host = await _create_host_record()
    entry = {
        "host": "api.acme.test",
        "credential_header": "Authorization",
        "credential_prefix": "Bearer ",
        "credential_var": "ACME_TOKEN",
        "endpoint_var": "ACME_BASE_URL",
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
    assert await _stored_secrets(host.id) == {"acme": {**entry, "base_path": ""}}


async def test_registration_records_the_placeholder_fingerprint(client) -> None:
    host = await _create_host_record()

    await client.put(
        f"/hosts/{host.id}/secrets/github", headers=AUTH_HEADERS, json={"value": "secret"}
    )

    assert len(await _placeholder_fingerprint(host.id, "github")) == 64


async def test_registration_on_an_active_host_delivers_the_placeholder(client, monkeypatch) -> None:
    put_secret = AsyncMock(return_value={})
    monkeypatch.setattr("providers.docker.provider.DockerProvider.put_secret", put_secret)
    host = await _create_host_record(provider="docker", status=HostStatus.ACTIVE.value)

    response = await client.put(
        f"/hosts/{host.id}/secrets/openai", headers=AUTH_HEADERS, json={"value": "secret"}
    )

    assert response.status_code == 204
    call = put_secret.await_args_list[0].kwargs
    assert call["vm"] == host.name
    assert call["service"] == {
        "name": "openai",
        "host": "api.openai.com",
        "credential_header": "Authorization",
        "credential_prefix": "Bearer ",
        "credential_var": "OPENAI_API_KEY",
        "endpoint_var": "OPENAI_BASE_URL",
        "base_path": "/v1",
    }
    placeholder = Placeholder.read(call["value"])
    assert (placeholder.host_id, placeholder.service) == (host.id, "openai")
    assert placeholder.matches(await _placeholder_fingerprint(host.id, "openai"))


async def test_a_service_without_a_base_url_variable_is_refused_on_an_active_host(
    client, monkeypatch
) -> None:
    put_secret = AsyncMock(return_value={})
    monkeypatch.setattr("providers.docker.provider.DockerProvider.put_secret", put_secret)
    host = await _create_host_record(provider="docker", status=HostStatus.ACTIVE.value)

    response = await client.put(
        f"/hosts/{host.id}/secrets/github", headers=AUTH_HEADERS, json={"value": "secret"}
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "SECRET_DELIVERY_UNSUPPORTED"
    put_secret.assert_not_awaited()
    assert await _stored_secrets(host.id) == {}


async def test_a_provider_with_its_own_edge_is_refused_until_its_adapter_delivers(
    client, monkeypatch
) -> None:
    put_secret = AsyncMock(return_value={})
    monkeypatch.setattr("providers.exe.provider.ExeProvider.put_secret", put_secret)
    host = await _create_host_record(provider="exe", status=HostStatus.ACTIVE.value)

    response = await client.put(
        f"/hosts/{host.id}/secrets/openai", headers=AUTH_HEADERS, json={"value": "secret"}
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "SECRET_DELIVERY_UNSUPPORTED"
    put_secret.assert_not_awaited()
    assert await _stored_secrets(host.id) == {}


async def test_a_provider_failure_during_delivery_is_a_502(client, monkeypatch) -> None:
    put_secret = AsyncMock(side_effect=ProviderCommandError("SECRETS_EXCHANGE_URL must be set"))
    monkeypatch.setattr("providers.docker.provider.DockerProvider.put_secret", put_secret)
    host = await _create_host_record(provider="docker", status=HostStatus.ACTIVE.value)

    response = await client.put(
        f"/hosts/{host.id}/secrets/openai", headers=AUTH_HEADERS, json={"value": "secret"}
    )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "SECRETS_EXCHANGE_URL must be set",
        "error_code": "SECRET_DELIVERY_FAILED",
    }
    assert await _stored_secrets(host.id) == {}


async def test_registration_on_an_active_host_that_cannot_deliver_is_refused(
    client, monkeypatch
) -> None:
    class BareProvider:
        name = "bare"

    monkeypatch.setattr("host_secrets.service.get_vm_provider", lambda name: BareProvider())
    host = await _create_host_record(provider="bare", status=HostStatus.ACTIVE.value)

    response = await client.put(
        f"/hosts/{host.id}/secrets/github", headers=AUTH_HEADERS, json={"value": "secret"}
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "SECRET_DELIVERY_UNSUPPORTED"
    assert await _stored_secrets(host.id) == {}


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


async def _create_host_record(
    *,
    secrets: dict[str, object] | None = None,
    provider: str = "exe",
    status: str = HostStatus.PROVISIONING.value,
) -> Host:
    now = utc_now()
    host = Host(
        name=f"sb-{uuid.uuid4().hex[:12]}",
        image="sandbox:latest",
        provider=provider,
        status=status,
        secrets=secrets or {},
        created_at=now,
        updated_at=now,
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()
    return host


async def _stored_secrets(host_id: uuid.UUID) -> dict[str, object]:
    """The stored entries as the caller registered them, without the placeholder fingerprint."""
    async with async_session_factory() as session:
        host = (await session.execute(select(Host).where(Host.id == host_id))).scalar_one()
        return {
            name: {key: value for key, value in entry.items() if key != "placeholder_fingerprint"}
            for name, entry in host.secrets.items()
        }


async def _placeholder_fingerprint(host_id: uuid.UUID, name: str) -> str:
    async with async_session_factory() as session:
        host = (await session.execute(select(Host).where(Host.id == host_id))).scalar_one()
        return host.secrets[name]["placeholder_fingerprint"]
