import base64
import logging
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from core.database import async_session_factory
from host_secrets.placeholder import Placeholder
from hosts.models import Host, HostStatus
from hosts.service import utc_now
from hosts.tests.conftest import stub_provider  # noqa: F401
from providers.registry import get_vm_provider
from secrets_exchange.app import UPSTREAM_CREDENTIAL, UPSTREAM_HEADER, app, push_held
from secrets_exchange.secrets import Secrets

ISSUER = {"url": "https://mint.test/box/anthropic", "headers": {}, "refresh": "1h"}


@pytest.fixture
async def edge() -> AsyncGenerator[AsyncClient]:
    # ASGITransport does not run the lifespan, so the test gives the app its secrets.
    async with (
        httpx.AsyncClient() as outbound,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://secrets exchange") as c,
    ):
        app.state.secrets = Secrets(outbound)
        yield c


async def test_a_placeholder_is_exchanged_for_the_real_secret(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    await _create_host(
        host_id, {"github": {"value": "ghs_real", "placeholder_fingerprint": minted.fingerprint}}
    )

    response = await edge.get("/authorize", headers=_headers(str(minted), "api.github.com"))

    assert response.status_code == 200
    assert response.headers["X-Upstream-Host"] == "api.github.com"
    assert response.headers["X-Upstream-Header"] == "Authorization"
    assert response.headers["X-Upstream-Credential"] == "Bearer ghs_real"


async def test_the_git_host_of_github_gets_basic_with_the_token_as_the_password(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    await _create_host(
        host_id, {"github": {"value": "ghs_real", "placeholder_fingerprint": minted.fingerprint}}
    )

    response = await edge.get("/authorize", headers=_headers(str(minted), "github.com"))

    assert response.status_code == 200
    assert response.headers["X-Upstream-Host"] == "github.com"
    assert response.headers["X-Upstream-Header"] == "Authorization"
    assert (
        response.headers["X-Upstream-Credential"]
        == "Basic " + base64.b64encode(b"x-access-token:ghs_real").decode()
    )


async def test_a_custom_service_gets_its_own_header_shape(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "acme")
    await _create_host(
        host_id,
        {
            "acme": {
                "host": "api.acme.test",
                "credential_header": "x-api-key",
                "credential_prefix": "",
                "credential_var": "ACME_TOKEN",
                "value": "ak_live",
                "placeholder_fingerprint": minted.fingerprint,
            }
        },
    )

    response = await edge.get("/authorize", headers=_headers(str(minted), "api.acme.test"))

    assert response.status_code == 200
    assert response.headers["X-Upstream-Host"] == "api.acme.test"
    assert response.headers["X-Upstream-Header"] == "x-api-key"
    assert response.headers["X-Upstream-Credential"] == "ak_live"


@respx.mock
async def test_an_issuer_entry_is_fetched_and_exchanged(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    issuer = {"url": "https://mint.test/x", "headers": {"X-Key": "k"}, "refresh": "1h"}
    await _create_host(
        host_id, {"github": {"issuer": issuer, "placeholder_fingerprint": minted.fingerprint}}
    )
    route = respx.get("https://mint.test/x").respond(json={"value": "ghs_minted"})

    response = await edge.get("/authorize", headers=_headers(str(minted), "api.github.com"))

    assert response.status_code == 200
    assert response.headers["X-Upstream-Credential"] == "Bearer ghs_minted"
    assert route.calls[0].request.headers["X-Key"] == "k"


@respx.mock
async def test_an_issuer_that_gives_nothing_usable_answers_503(edge) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    issuer = {"url": "https://mint.test/x", "headers": {"X-Key": "k"}, "refresh": "1h"}
    await _create_host(
        host_id, {"github": {"issuer": issuer, "placeholder_fingerprint": minted.fingerprint}}
    )
    respx.get("https://mint.test/x").respond(status_code=502)

    response = await edge.get("/authorize", headers=_headers(str(minted), "api.github.com"))

    assert response.status_code == 503
    assert response.headers["Retry-After"]
    assert "X-Upstream-Credential" not in response.headers


@pytest.mark.parametrize(
    ("placeholder", "host"),
    [
        ("", "api.github.com"),
        ("sk-ant-oat01-something", "api.github.com"),
        ("drk.{host}.github.wrong-secret", "api.github.com"),
        ("drk.{other}.github.{secret}", "api.github.com"),
        ("drk.{host}.openai.{secret}", "api.openai.com"),
        ("drk.{host}.github.{secret}", "api.anthropic.com"),
        ("drk.{host}.github.{secret}", ""),
    ],
)
async def test_anything_else_is_refused_with_403_never_401(
    edge, placeholder: str, host: str
) -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")
    await _create_host(
        host_id, {"github": {"value": "ghs_real", "placeholder_fingerprint": minted.fingerprint}}
    )
    presented = placeholder.format(host=host_id.hex, other=uuid.uuid4().hex, secret=minted.secret)

    response = await edge.get("/authorize", headers=_headers(presented, host))

    assert response.status_code == 403
    assert "X-Upstream-Credential" not in response.headers


async def test_healthz(edge) -> None:
    assert (await edge.get("/healthz")).status_code == 200


async def test_the_upstreams_are_the_hosts_with_a_registered_secret(edge) -> None:
    await _create_host(uuid.uuid4(), {"anthropic": {"value": "sk-ant-real"}})
    await _create_host(
        uuid.uuid4(),
        {
            "anthropic": {"value": "sk-ant-other"},
            "acme": {
                "host": "api.acme.test",
                "credential_header": "x-api-key",
                "credential_prefix": "",
                "credential_var": "ACME_TOKEN",
                "value": "ak_live",
            },
        },
    )
    await _create_host(uuid.uuid4(), {"github": {"value": "ghs_real"}})
    await _create_host(uuid.uuid4(), {})

    response = await edge.get("/upstreams")

    assert response.status_code == 200
    assert response.json() == [
        "api.acme.test",
        "api.anthropic.com",
        "api.github.com",
        "github.com",
        "uploads.github.com",
    ]


async def test_no_secret_means_no_upstream(edge) -> None:
    assert (await edge.get("/upstreams")).json() == []


def test_the_proxy_addon_reads_the_headers_the_exchange_answers_with() -> None:
    addon = (Path(__file__).parents[3] / "deploy" / "proxy" / "swap.py").read_text()

    assert f'UPSTREAM_HEADER = "{UPSTREAM_HEADER}"' in addon
    assert f'UPSTREAM_CREDENTIAL = "{UPSTREAM_CREDENTIAL}"' in addon


def _headers(placeholder: str, host: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {placeholder}", "X-Forwarded-Host": host}


async def _create_host(
    host_id: uuid.UUID, secrets: dict[str, object], provider: str = "exe"
) -> None:
    now = utc_now()
    async with async_session_factory() as session:
        session.add(
            Host(
                id=host_id,
                name=f"sb-{host_id.hex[:12]}",
                image="sandbox:latest",
                provider=provider,
                status=HostStatus.ACTIVE.value,
                secrets=secrets,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


@respx.mock
@pytest.mark.usefixtures("stub_provider")
async def test_the_timer_pushes_issuer_values_to_a_provider_that_holds_them(edge) -> None:
    injection = MagicMock(holds_value=True)
    injection.push_secret = AsyncMock()
    get_vm_provider("stub").secret_injection = injection
    host_id = uuid.uuid4()
    await _create_host(
        host_id,
        {
            "anthropic": {"issuer": ISSUER, "placeholder_fingerprint": "a"},
            "github": {"value": "ghs_real", "placeholder_fingerprint": "b"},
        },
        provider="stub",
    )
    respx.get(ISSUER["url"]).respond(json={"value": "sk-ant-fresh"})

    await push_held(app.state.secrets)

    injection.push_secret.assert_awaited_once_with(
        vm=f"sb-{host_id.hex[:12]}", name="anthropic", value="sk-ant-fresh"
    )


@respx.mock
@pytest.mark.usefixtures("stub_provider")
async def test_the_timer_forgets_a_deleted_host(edge) -> None:
    injection = MagicMock(holds_value=True)
    injection.push_secret = AsyncMock()
    get_vm_provider("stub").secret_injection = injection
    host_id = uuid.uuid4()
    await _create_host(
        host_id, {"anthropic": {"issuer": ISSUER, "placeholder_fingerprint": "a"}}, provider="stub"
    )
    respx.get(ISSUER["url"]).respond(json={"value": "sk-ant-fresh"})
    await push_held(app.state.secrets)
    assert (host_id, "anthropic") in app.state.secrets._refreshable

    async with async_session_factory() as session:
        await session.delete(await session.get(Host, host_id))
        await session.commit()
    await push_held(app.state.secrets)

    assert (host_id, "anthropic") not in app.state.secrets._refreshable
    injection.push_secret.assert_awaited_once()


@respx.mock
@pytest.mark.usefixtures("stub_provider")
async def test_one_host_in_trouble_costs_no_other_host_its_value(edge, caplog) -> None:
    injection = MagicMock(holds_value=True)
    injection.push_secret = AsyncMock()
    get_vm_provider("stub").secret_injection = injection
    troubled, healthy = uuid.uuid4(), uuid.uuid4()
    entry = {"issuer": ISSUER, "placeholder_fingerprint": "a"}
    await _create_host(troubled, {"anthropic": entry}, provider="gone")
    await _create_host(healthy, {"anthropic": entry}, provider="stub")
    respx.get(ISSUER["url"]).respond(json={"value": "sk-ant-fresh"})

    with caplog.at_level(logging.ERROR):
        await push_held(app.state.secrets)

    injection.push_secret.assert_awaited_once_with(
        vm=f"sb-{healthy.hex[:12]}", name="anthropic", value="sk-ant-fresh"
    )
    assert f"sb-{troubled.hex[:12]}" in caplog.text
