import inspect

import pytest

from providers.capabilities import SecretInjectionCapability, resolve_capability
from providers.exceptions import CapabilityUnsupportedError
from providers.registry import get_vm_provider


def test_secret_injection_capability_has_one_box_scoped_lifecycle() -> None:
    """Providers implement only put, delete, and list operations."""
    assert SecretInjectionCapability.__abstractmethods__ == {
        "put_secret",
        "delete_secret",
        "list_secrets",
    }


def test_secret_injection_capability_has_exact_method_signatures() -> None:
    assert tuple(inspect.signature(SecretInjectionCapability.put_secret).parameters) == (
        "self",
        "vm",
        "name",
        "host",
        "auth_var",
        "base_url_var",
        "placeholder",
        "value",
    )
    assert tuple(inspect.signature(SecretInjectionCapability.delete_secret).parameters) == (
        "self",
        "vm",
        "name",
    )
    assert tuple(inspect.signature(SecretInjectionCapability.list_secrets).parameters) == (
        "self",
        "vm",
    )


def test_resolve_capability_returns_implementing_provider() -> None:
    """A provider that inherits the capability mix-in resolves to itself."""
    provider = get_vm_provider("exe")
    assert resolve_capability(provider, SecretInjectionCapability) is provider


def test_resolve_capability_refuses_provider_without_capability() -> None:
    """A provider without the mix-in raises the shared unsupported error."""
    with pytest.raises(CapabilityUnsupportedError, match="'docker' does not support"):
        resolve_capability(get_vm_provider("docker"), SecretInjectionCapability)
