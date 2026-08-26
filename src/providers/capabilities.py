import abc
from typing import TypeVar

from providers.base import VMProvider
from providers.exceptions import CapabilityUnsupportedError

CapabilityT = TypeVar("CapabilityT")


def resolve_capability(provider: VMProvider, capability: type[CapabilityT]) -> CapabilityT:
    if not isinstance(provider, capability):
        raise CapabilityUnsupportedError(
            f"VM provider '{provider.name}' does not support {capability.__name__}",
        )
    return provider


class HttpProxyCapability(abc.ABC):
    """Mix-in declaring a VMProvider also speaks the http-proxy capability.

    Modeled as an ABC (not a Protocol) so isinstance() really checks the
    inheritance chain — runtime_checkable Protocols would let any object that
    happens to expose the four method names pass the check, including MagicMock
    instances and providers with mismatched signatures.
    """

    @abc.abstractmethod
    async def create_http_proxy(
        self,
        *,
        name: str,
        target: str,
        headers: dict[str, str],
    ) -> None: ...

    @abc.abstractmethod
    async def delete_http_proxy(self, name: str) -> None: ...

    @abc.abstractmethod
    async def attach_http_proxy(self, name: str, *, attach_vm: str) -> None: ...

    @abc.abstractmethod
    async def detach_http_proxy(self, name: str, *, attach_vm: str) -> None: ...


class TemplateCapability(abc.ABC):
    """Mix-in declaring a VMProvider can build and delete template images.

    Modeled as an ABC so resolve_capability checks the inheritance chain
    instead of accepting any object that has these method names.
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
