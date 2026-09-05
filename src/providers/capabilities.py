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

    ``put_secret`` takes the service being reached and the secret to reach it
    with, and returns the environment the VM should be given. Providers differ
    in what that environment is: one hands the VM a stand-in credential and
    leaves the address alone, another hands it a different address and no
    credential. Callers apply whatever comes back without knowing which.

    A service describes how a client of it is configured:

    ``name``                the service handle, unique per VM
    ``host``                the real upstream, without a scheme
    ``credential_header``   the header the service authenticates with
    ``credential_prefix``   what precedes the value in that header, often empty
    ``credential_var``      the variable a client reads the credential from
    ``endpoint_var``        the variable a client reads the base URL from

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
        service: dict[str, str],
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
