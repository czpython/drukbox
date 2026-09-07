import abc
from typing import ClassVar, TypeVar

from core.settings import get_settings
from host_secrets.catalog import Service
from host_secrets.placeholder import Placeholder
from providers.exceptions import CapabilityUnsupportedError

CapabilityT = TypeVar("CapabilityT")

# The box itself and the cloud metadata address.
NO_PROXY = "localhost,127.0.0.1,::1,169.254.169.254"


def resolve_capability(provider, capability: type[CapabilityT]) -> CapabilityT:
    if not isinstance(provider, capability):
        raise CapabilityUnsupportedError(
            f"VM provider '{provider.name}' does not support {capability.__name__}",
        )
    return provider


class SecretInjectionCapability(abc.ABC):
    """How a secret reaches one provider's boxes. ``put_secret`` returns the
    environment the box needs."""

    # sbx keeps the value in its own store. The proxy needs only the placeholder.
    needs_value: ClassVar[bool]

    @abc.abstractmethod
    async def put_secret(
        self,
        *,
        vm: str,
        service: Service,
        placeholder: Placeholder,
        value: str,
    ) -> dict[str, str]: ...

    @abc.abstractmethod
    async def delete_secret(self, *, vm: str, placeholder: Placeholder) -> None: ...


class ProxyInjection(SecretInjectionCapability):
    """The box sends its HTTPS through the proxy, which swaps the placeholder."""

    needs_value = False

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
            service["auth_variable"]: str(placeholder),
            "HTTPS_PROXY": proxy,
            "https_proxy": proxy,
            "NO_PROXY": NO_PROXY,
        }

    async def delete_secret(self, *, vm: str, placeholder: Placeholder) -> None:
        return


class TemplateCapability(abc.ABC):
    """An ABC, not a Protocol, so ``resolve_capability`` can test inheritance."""

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
