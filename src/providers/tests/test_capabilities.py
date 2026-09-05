import pytest

from providers.base import VMCreateResult, VMProvider
from providers.capabilities import SecretInjectionCapability, resolve_capability
from providers.exceptions import CapabilityUnsupportedError
from providers.registry import get_vm_provider


class StubInjectingProvider(SecretInjectionCapability, VMProvider):
    """The smallest provider that injects secrets. These tests say nothing about
    any real provider."""

    name = "injecting-stub"
    diagnose_hint = "check_injecting_stub"

    @classmethod
    def from_settings(cls) -> "StubInjectingProvider":
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

    async def put_secret(self, *, vm: str, service: dict[str, str], value: str) -> dict[str, str]:
        return {service["credential_var"]: value}

    async def delete_secret(self, *, vm: str, name: str) -> None:
        return

    async def list_secrets(self, *, vm: str) -> list[str]:
        return []


def test_resolve_capability_returns_implementing_provider() -> None:
    provider = StubInjectingProvider()
    assert resolve_capability(provider, SecretInjectionCapability) is provider


def test_resolve_capability_refuses_provider_without_capability() -> None:
    with pytest.raises(CapabilityUnsupportedError, match="'docker' does not support"):
        resolve_capability(get_vm_provider("docker"), SecretInjectionCapability)
