import abc
from typing import ClassVar, TypeVar

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
    """Mix-in that declares that a VMProvider can keep secrets outside its VMs.

    ``put_secret`` takes the service to reach and the value the VM sends. A
    provider that uses the secrets exchange (``uses_secrets_exchange``)
    receives a placeholder, and the exchange swaps it for the secret. A
    provider with its own edge receives the secret itself. It returns the
    environment that the VM needs. Providers differ in what that environment
    holds. One provider gives the VM
    a stand-in credential and leaves the address unchanged. Another gives the
    VM a different address and no credential. A caller applies what comes back
    and never learns which ran.

    A service describes how a client of it reads its configuration:

    ``name``                The service handle, unique per VM
    ``host``                The real upstream, without a scheme
    ``credential_header``   The header that the service authenticates with
    ``credential_prefix``   What comes before the value in that header
    ``credential_var``      The variable that a client reads the credential from
    ``endpoint_var``        The variable that a client reads the base URL from
    ``base_path``           What the client expects after the host in that base URL

    This is an ABC and not a Protocol, so isinstance() tests the inheritance
    chain. A runtime-checkable Protocol accepts any object with these three
    method names. That includes a MagicMock and a provider with wrong signatures.
    """

    uses_secrets_exchange: ClassVar[bool] = True

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
