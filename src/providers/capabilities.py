import abc
from typing import TypeVar

from core.settings import get_settings
from host_secrets.catalog import Service
from host_secrets.placeholder import Placeholder
from providers.base import SecretInjectionCapability, VMProvider
from providers.exceptions import CapabilityUnsupportedError

CapabilityT = TypeVar("CapabilityT")

# What a box reaches without the proxy: itself, and the cloud metadata address.
NO_PROXY = "localhost,127.0.0.1,::1,169.254.169.254"


def resolve_capability(provider: VMProvider, capability: type[CapabilityT]) -> CapabilityT:
    if not isinstance(provider, capability):
        raise CapabilityUnsupportedError(
            f"VM provider '{provider.name}' does not support {capability.__name__}",
        )
    return provider


class ProxyInjection(SecretInjectionCapability):
    """The box holds the placeholder and sends its HTTPS through our proxy.

    The proxy swaps the placeholder for the real value on the way out. The
    value never leaves the exchange, so nothing is pushed anywhere and there
    is nothing to delete.
    """

    holds_value = False

    async def put_secret(
        self,
        *,
        vm: str,
        service: Service,
        placeholder: Placeholder,
        value: str,
    ) -> dict[str, str]:
        proxy = get_settings().secrets_proxy_url
        return {
            service["credential_var"]: str(placeholder),
            "HTTPS_PROXY": proxy,
            "https_proxy": proxy,
            "NO_PROXY": NO_PROXY,
        }

    async def delete_secret(self, *, vm: str, placeholder: Placeholder) -> None:
        return


class TemplateCapability(abc.ABC):
    """Mix-in that declares that a VMProvider can build and delete template images.

    This is an ABC so that resolve_capability tests the inheritance chain. A
    Protocol accepts any object that has these method names.
    """

    @abc.abstractmethod
    async def build_template_image(
        self,
        *,
        base_image: str,
        setup_script: str,
        label: str,
    ) -> str: ...

    @abc.abstractmethod
    async def delete_template_image(self, image: str) -> None: ...
