import pytest

from providers.base import VMCreateResult, VMProvider
from providers.capabilities import TemplateCapability, resolve_capability
from providers.exceptions import CapabilityUnsupportedError


class StubProvider(VMProvider):
    """The smallest provider. These tests say nothing about any real provider."""

    name = "stub"
    diagnose_hint = "check_stub"

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
