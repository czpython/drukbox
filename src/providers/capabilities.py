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


class SecretInjectionCapability(abc.ABC):
    """Mix-in declaring that a VMProvider can keep secrets outside its VMs.

    Modeled as an ABC (not a Protocol) so isinstance() really checks the
    inheritance chain. Runtime-checkable protocols would let any object that
    happens to expose the three method names pass the check, including MagicMock
    instances and providers with mismatched signatures.
    """

    @abc.abstractmethod
    async def put_secret(
        self,
        *,
        vm: str,
        name: str,
        host: str,
        auth_var: str,
        base_url_var: str,
        placeholder: str,
        value: str,
    ) -> dict[str, str]: ...

    @abc.abstractmethod
    async def delete_secret(self, *, vm: str, name: str) -> None: ...

    @abc.abstractmethod
    async def list_secrets(self, *, vm: str) -> list[str]: ...


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
