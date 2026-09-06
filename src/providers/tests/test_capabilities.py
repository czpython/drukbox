import base64
import uuid
from pathlib import Path

import pytest

from core.settings import get_settings
from host_secrets.catalog import CATALOG
from host_secrets.placeholder import Placeholder
from providers.aws.provider import AWSProvider
from providers.base import VMCreateResult, VMProvider
from providers.capabilities import ProxyInjection, TemplateCapability, resolve_capability
from providers.docker.provider import DockerProvider
from providers.docker_sbx.provider import DockerSbxProvider
from providers.environment import persist
from providers.exceptions import CapabilityUnsupportedError, ProviderCommandError
from providers.exe.provider import ExeProvider
from providers.exoscale.provider import ExoscaleProvider
from providers.hetzner.provider import HetznerProvider

TEST_CERTIFICATE = Path(__file__).parents[3] / "env" / "test-proxy-ca.pem"


class StubProvider(VMProvider):
    """The smallest provider. These tests say nothing about any real provider."""

    name = "stub"
    diagnose_hint = "check_stub"
    secret_injection = ProxyInjection()

    @classmethod
    def from_settings(cls) -> "StubProvider":
        return cls()

    @property
    def default_image(self) -> str:
        return "stub:base"

    @property
    def bootstrap_ssh_timeout_seconds(self) -> float:
        return 0.1

    async def create_vm(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str] | None = None,
        setup_script: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
    ) -> VMCreateResult:
        return VMCreateResult(provider_id=name, name=name, ssh_port=22, ssh_username="stub")

    async def delete_vm(self, name: str) -> None:
        return

    async def diagnose(self) -> str:
        return "stub ok"

    async def aclose(self) -> None:
        return


class StubTemplateProvider(StubProvider, TemplateCapability):
    async def build_template_image(self, *, base_image: str, setup_script: str, label: str) -> str:
        return f"{base_image}:{label}"

    async def delete_template_image(self, image: str) -> None:
        return


def test_resolve_capability_returns_implementing_provider() -> None:
    provider = StubTemplateProvider()
    assert resolve_capability(provider, TemplateCapability) is provider


def test_resolve_capability_refuses_provider_without_capability() -> None:
    with pytest.raises(CapabilityUnsupportedError, match="'stub' does not support"):
        resolve_capability(StubProvider(), TemplateCapability)


async def test_the_proxy_injection_hands_the_box_its_placeholder_the_proxy_and_the_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    certificate = tmp_path / "ca.pem"
    certificate.write_bytes(TEST_CERTIFICATE.read_bytes())
    monkeypatch.setattr(get_settings(), "secrets_proxy_url", "http://proxy.test:8880")
    monkeypatch.setattr(get_settings(), "secrets_proxy_ca_file", str(certificate))
    placeholder = Placeholder.mint(uuid.uuid4(), "anthropic")

    environment = await ProxyInjection().put_secret(
        vm="sb-one", service=CATALOG["anthropic"], placeholder=placeholder, value=""
    )

    assert environment == {
        "ANTHROPIC_AUTH_TOKEN": str(placeholder),
        "HTTPS_PROXY": "http://proxy.test:8880",
        "https_proxy": "http://proxy.test:8880",
        "NO_PROXY": "localhost,127.0.0.1,::1,169.254.169.254",
        "SECRETS_PROXY_CA": base64.b64encode(certificate.read_bytes()).decode(),
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "CURL_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "NODE_EXTRA_CA_CERTS": "/usr/local/share/ca-certificates/drukbox.crt",
    }
    # Every value must survive pam_env, the base64 certificate included.
    persist(environment)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "does not name a readable certificate"),
        (b"not a certificate", "does not name a readable certificate"),
        (
            b"-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIA==\n-----END PRIVATE KEY-----\n",
            "does not name a readable certificate",
        ),
        (
            b"-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIA==\n-----END PRIVATE KEY-----\n"
            + TEST_CERTIFICATE.read_bytes(),
            "holds a private key",
        ),
    ],
)
async def test_the_proxy_injection_hands_out_a_public_certificate_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes | None, reason: str
) -> None:
    path = tmp_path / "ca.pem"
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setattr(get_settings(), "secrets_proxy_ca_file", str(path))

    with pytest.raises(ProviderCommandError, match=reason):
        await ProxyInjection().put_secret(
            vm="sb-one",
            service=CATALOG["anthropic"],
            placeholder=Placeholder.mint(uuid.uuid4(), "anthropic"),
            value="",
        )


async def test_the_proxy_injection_holds_no_value_and_has_nothing_to_delete() -> None:
    assert ProxyInjection.holds_value is False

    await ProxyInjection().delete_secret(
        vm="sb-one", placeholder=Placeholder.mint(uuid.uuid4(), "anthropic")
    )


@pytest.mark.parametrize(
    "provider",
    [DockerProvider, ExeProvider, AWSProvider, HetznerProvider, ExoscaleProvider],
)
def test_every_provider_but_docker_sbx_injects_through_the_proxy(
    provider: type[VMProvider],
) -> None:
    assert type(provider.secret_injection) is ProxyInjection


def test_docker_sbx_declares_its_own_injection_per_instance() -> None:
    # sbx's implementation needs the CLI and the workspace root, so the
    # provider constructs it. The class carries only the annotation.
    assert "secret_injection" not in vars(DockerSbxProvider)
