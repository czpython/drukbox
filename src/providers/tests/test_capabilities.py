import uuid

import pytest

from core.settings import get_settings
from host_secrets.catalog import CATALOG
from host_secrets.placeholder import Placeholder
from providers.aws.provider import AWSProvider
from providers.base import VMCreateResult, VMProvider
from providers.capabilities import ProxyInjection, TemplateCapability, resolve_capability
from providers.docker.provider import DockerProvider
from providers.docker_sbx.provider import DockerSbxProvider
from providers.exceptions import CapabilityUnsupportedError
from providers.exe.provider import ExeProvider
from providers.exoscale.provider import ExoscaleProvider
from providers.hetzner.provider import HetznerProvider


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


async def test_the_proxy_injection_hands_the_box_its_placeholder_and_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "secrets_proxy_url", "http://proxy.test:8880")
    placeholder = Placeholder.mint(uuid.uuid4(), "anthropic")

    environment = await ProxyInjection().put_secret(
        vm="sb-one", service=CATALOG["anthropic"], placeholder=placeholder, value=""
    )

    assert environment == {
        "ANTHROPIC_AUTH_TOKEN": str(placeholder),
        "HTTPS_PROXY": "http://proxy.test:8880",
        "https_proxy": "http://proxy.test:8880",
        "NO_PROXY": "localhost,127.0.0.1,::1,169.254.169.254",
    }


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
