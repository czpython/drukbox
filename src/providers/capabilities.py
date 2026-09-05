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
