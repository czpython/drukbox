import abc
import base64
import pathlib
from typing import TypeVar

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from core.settings import get_settings
from host_secrets.catalog import Service
from host_secrets.placeholder import Placeholder
from providers import environment
from providers.base import SecretInjectionCapability, VMProvider
from providers.exceptions import CapabilityUnsupportedError, ProviderCommandError

CapabilityT = TypeVar("CapabilityT")

# What a box reaches without the proxy: itself, and the cloud metadata address.
NO_PROXY = "localhost,127.0.0.1,::1,169.254.169.254"
# Where update-ca-certificates puts the system bundle, with our CA in it.
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def public_certificate(path: str) -> bytes:
    """The public certificate in the PEM file at ``path``, and nothing else.

    mitmproxy writes the CA's key and certificate into one file and the
    certificate alone into another. Only the second may reach a box.
    """
    try:
        pem = pathlib.Path(path).read_bytes()
        certificate = x509.load_pem_x509_certificate(pem)
    except (OSError, ValueError) as exc:
        raise ProviderCommandError(
            f"SECRETS_PROXY_CA_FILE does not name a readable certificate: {exc}"
        ) from exc
    if b"PRIVATE KEY" in pem:
        raise ProviderCommandError(
            "SECRETS_PROXY_CA_FILE holds a private key. Name the public certificate only."
        )
    return certificate.public_bytes(Encoding.PEM)


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
    is nothing to delete. The box gets the proxy's CA with the address, and
    the variables that point curl, Python, and Node at it.
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
        settings = get_settings()
        return {
            service.credential_var: str(placeholder),
            "HTTPS_PROXY": settings.secrets_proxy_url,
            "https_proxy": settings.secrets_proxy_url,
            "NO_PROXY": NO_PROXY,
            environment.PROXY_CA: base64.b64encode(
                public_certificate(settings.secrets_proxy_ca_file)
            ).decode(),
            "SSL_CERT_FILE": SYSTEM_CA_BUNDLE,
            "REQUESTS_CA_BUNDLE": SYSTEM_CA_BUNDLE,
            "CURL_CA_BUNDLE": SYSTEM_CA_BUNDLE,
            "NODE_EXTRA_CA_CERTS": environment.PROXY_CA_PATH,
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
