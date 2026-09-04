import inspect
from unittest.mock import MagicMock

import pytest

from providers.aws.provider import AWSProvider
from providers.capabilities import SecretInjectionCapability, resolve_capability
from providers.docker.provider import DockerProvider
from providers.exceptions import CapabilityUnsupportedError
from providers.exe.provider import ExeProvider
from providers.exoscale.provider import ExoscaleProvider
from providers.hetzner.provider import HetznerProvider
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


@pytest.mark.parametrize(
    "provider_class",
    [
        AWSProvider,
        DockerProvider,
        ExeProvider,
        ExoscaleProvider,
        HetznerProvider,
    ],
)
def test_providers_with_a_secret_edge_implement_secret_injection(
    provider_class: type[SecretInjectionCapability],
) -> None:
    assert issubclass(provider_class, SecretInjectionCapability)


def test_resolve_capability_refuses_provider_without_capability() -> None:
    """A provider without the mix-in raises the shared unsupported error."""
    provider = MagicMock()
    provider.name = "plain"
    with pytest.raises(CapabilityUnsupportedError, match="'plain' does not support"):
        resolve_capability(provider, SecretInjectionCapability)
