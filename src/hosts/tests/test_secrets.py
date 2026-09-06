import logging
from collections.abc import Generator
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import select, text

from core.database import async_session_factory
from core.settings import Settings, get_settings
from host_secrets.catalog import Service
from host_secrets.placeholder import Placeholder
from hosts.exceptions import ProvisioningFailedError
from hosts.models import Host, HostStatus
from hosts.service import HostService, utc_now
from hosts.tests.conftest import StubVMProvider
from providers.base import SecretInjectionCapability, VMCreateResult

ISSUER = {"url": "https://mint.test/box/github", "headers": {"X-Key": "k"}, "refresh": "1h"}
SECRETS = {
    "anthropic": {"value": "sk-ant-real"},
    "github": {
        "host": "api.github.com",
        "credential_header": "Authorization",
        "credential_prefix": "Bearer ",
        "credential_var": "GH_TOKEN",
        "issuer": ISSUER,
    },
}


class RecordingInjection(SecretInjectionCapability):
    """Holds the value, as sbx does. Records what it was handed."""

    holds_value = True

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def put_secret(
        self, *, vm: str, service: Service, placeholder: Placeholder, value: str
    ) -> dict[str, str]:
        self.values[placeholder.service] = value
        return {service["credential_var"]: str(placeholder)}

    async def delete_secret(self, *, vm: str, placeholder: Placeholder) -> None:
        return


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Generator[Settings, None, None]:
    monkeypatch.setenv("TAILSCALE_ENABLED", "false")
    monkeypatch.setenv("SECRETS_PROXY_URL", "http://proxy.test:8880")
    monkeypatch.setenv("SECRETS_PROXY_CA_FILE", "env/test-proxy-ca.pem")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def create_vm(monkeypatch: pytest.MonkeyPatch, stub_provider: StubVMProvider) -> AsyncMock:
    create_vm = AsyncMock(
        return_value=VMCreateResult(
            provider_id="sb-x",
            name="sb-x",
            ssh_port=22,
            ssh_username="stub",
            ssh_host="203.0.113.1",
        )
    )
    monkeypatch.setattr(stub_provider, "create_vm", create_vm)
    monkeypatch.setattr(
        "hosts.service.HostService.scan_known_hosts",
        AsyncMock(return_value=b"203.0.113.1 ssh-ed25519 AAAA\n"),
    )
    return create_vm


async def test_host_secrets_are_ciphertext_at_rest_and_decrypt_on_read() -> None:
    secrets = {
        "issuer": {
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


@respx.mock
async def test_a_proxy_box_boots_with_its_placeholders_and_the_proxy_and_no_value(
    settings: Settings, create_vm: AsyncMock
) -> None:
    issuer = respx.get(ISSUER["url"])

    async with async_session_factory() as session:
        host = await HostService(session, settings=settings).create_host(
            env={"KEEP": "me"}, secrets=SECRETS, image=None, provider="stub"
        )

    environment = _boot_environment(create_vm)
    assert environment["KEEP"] == "me"
    assert environment["HTTPS_PROXY"] == environment["https_proxy"] == "http://proxy.test:8880"
    assert environment["NO_PROXY"].startswith("localhost,")
    assert environment["SECRETS_PROXY_CA"]
    assert environment["NODE_EXTRA_CA_CERTS"] == "/usr/local/share/ca-certificates/drukbox.crt"
    for name, variable in (("anthropic", "ANTHROPIC_AUTH_TOKEN"), ("github", "GH_TOKEN")):
        placeholder = Placeholder.read(environment[variable])
        assert (placeholder.host_id, placeholder.service) == (host.id, name)
        assert placeholder.matches((await _stored_secrets(host))[name]["placeholder_fingerprint"])
    assert "sk-ant-real" not in environment.values()
    assert issuer.call_count == 0, "the value stays in the exchange, so nothing fetches it"
    assert host.status == HostStatus.ACTIVE.value


@respx.mock
async def test_a_provider_that_holds_the_value_gets_it_at_boot(
    settings: Settings, create_vm: AsyncMock, stub_provider: StubVMProvider
) -> None:
    recording = RecordingInjection()
    stub_provider.secret_injection = recording
    issuer = respx.get(ISSUER["url"]).respond(json={"value": "ghs_minted"})

    async with async_session_factory() as session:
        await HostService(session, settings=settings).create_host(
            env={}, secrets=SECRETS, image=None, provider="stub"
        )

    assert recording.values == {"anthropic": "sk-ant-real", "github": "ghs_minted"}
    assert issuer.calls[0].request.headers["X-Key"] == "k"
    environment = _boot_environment(create_vm)
    assert Placeholder.read(environment["GH_TOKEN"]).service == "github"
    assert "HTTPS_PROXY" not in environment


class OversizedInjection(RecordingInjection):
    """Hands the box a value that pam_env cannot read."""

    async def put_secret(
        self, *, vm: str, service: Service, placeholder: Placeholder, value: str
    ) -> dict[str, str]:
        return {service["credential_var"]: "a" * 9000}


async def test_a_boot_environment_pam_cannot_read_fails_provisioning_before_the_vm(
    settings: Settings, create_vm: AsyncMock, stub_provider: StubVMProvider
) -> None:
    stub_provider.secret_injection = OversizedInjection()

    async with async_session_factory() as session:
        with pytest.raises(ProvisioningFailedError, match="longer than 8191 bytes"):
            await HostService(session, settings=settings).create_host(
                env={}, secrets={"anthropic": {"value": "sk-ant-real"}}, image=None, provider="stub"
            )

    create_vm.assert_not_awaited()


@respx.mock
async def test_an_issuer_that_gives_no_value_at_boot_fails_provisioning(
    settings: Settings, create_vm: AsyncMock, stub_provider: StubVMProvider
) -> None:
    stub_provider.secret_injection = RecordingInjection()
    respx.get(ISSUER["url"]).respond(status_code=500)

    async with async_session_factory() as session:
        with pytest.raises(ProvisioningFailedError, match="IssuerError: status 500"):
            await HostService(session, settings=settings).create_host(
                env={}, secrets=SECRETS, image=None, provider="stub"
            )

    create_vm.assert_not_awaited()


@respx.mock
async def test_a_wrong_issuer_answer_at_boot_never_reaches_the_log_or_the_host(
    settings: Settings,
    create_vm: AsyncMock,
    stub_provider: StubVMProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub_provider.secret_injection = RecordingInjection()
    respx.get(ISSUER["url"]).respond(json={"access_token": "secret-xyz"})

    async with async_session_factory() as session:
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(ProvisioningFailedError, match="answer has the wrong shape") as failure,
        ):
            await HostService(session, settings=settings).create_host(
                env={}, secrets=SECRETS, image=None, provider="stub"
            )

    assert "secret-xyz" not in caplog.text
    assert "secret-xyz" not in str(failure.value)


async def test_secrets_on_a_provider_that_holds_the_value_need_no_proxy(
    client, monkeypatch: pytest.MonkeyPatch, stub_provider: StubVMProvider
) -> None:
    monkeypatch.setattr(get_settings(), "secrets_proxy_url", "")
    stub_provider.secret_injection = RecordingInjection()
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"provider": "stub", "secrets": {"anthropic": {"value": "sk-ant-real"}}},
    )

    assert response.status_code == 201


def _boot_environment(create_vm: AsyncMock) -> dict[str, str]:
    assert create_vm.await_args
    return create_vm.await_args.kwargs["env"]


async def _stored_secrets(host: Host) -> dict[str, dict[str, str]]:
    async with async_session_factory() as session:
        stored = await session.get(Host, host.id)
        assert stored
        return dict(stored.secrets)
