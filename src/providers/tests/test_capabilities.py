import pytest

from providers.capabilities import SecretInjectionCapability, resolve_capability
from providers.exceptions import CapabilityUnsupportedError
from providers.exe.provider import ExeProvider
from providers.registry import get_vm_provider


class SecretInjectingExeProvider(ExeProvider, SecretInjectionCapability):
    async def put_secret(
        self,
        *,
        vm: str,
        host: str,
        env_var: str,
        base_url_env: dict[str, str],
        headers: dict[str, str],
        placeholder: str,
        value: str,
    ) -> dict[str, str]:
        return {env_var: placeholder, **base_url_env}

    async def delete_secret(self, *, vm: str, env_var: str) -> None:
        return

    async def list_secrets(self, *, vm: str) -> list[str]:
        return []


def test_secret_injection_capability_has_one_box_scoped_lifecycle() -> None:
    """Providers implement only put, delete, and list operations."""
    assert SecretInjectionCapability.__abstractmethods__ == {
        "put_secret",
        "delete_secret",
        "list_secrets",
    }


def test_resolve_capability_returns_implementing_provider() -> None:
    """A provider that inherits the capability mix-in resolves to itself."""
    provider = SecretInjectingExeProvider.from_settings()
    assert resolve_capability(provider, SecretInjectionCapability) is provider


def test_resolve_capability_refuses_provider_without_capability() -> None:
    """A provider without the mix-in raises the shared unsupported error."""
    with pytest.raises(CapabilityUnsupportedError, match="'docker' does not support"):
        resolve_capability(get_vm_provider("docker"), SecretInjectionCapability)
