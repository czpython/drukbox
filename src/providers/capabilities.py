import abc
import base64
import pathlib
from typing import ClassVar, TypeVar

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from core.settings import get_settings
from host_secrets.catalog import Service
from host_secrets.placeholder import Placeholder
from providers import environment
from providers.exceptions import CapabilityUnsupportedError, ProviderCommandError

CapabilityT = TypeVar("CapabilityT")

# The box itself and the cloud metadata address.
NO_PROXY = "localhost,127.0.0.1,::1,169.254.169.254"
# update-ca-certificates writes the system bundle here.
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


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
    """The box sends its HTTPS through the proxy, which swaps the placeholder.
    It gets the proxy's CA and the trust variables."""

    needs_value = False

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
            service.auth_variable: str(placeholder),
            "HTTPS_PROXY": settings.secrets_proxy_url,
            "https_proxy": settings.secrets_proxy_url,
            "NO_PROXY": NO_PROXY,
            environment.PROXY_CA: base64.b64encode(self.get_public_certificate()).decode(),
            "SSL_CERT_FILE": SYSTEM_CA_BUNDLE,
            "REQUESTS_CA_BUNDLE": SYSTEM_CA_BUNDLE,
            "CURL_CA_BUNDLE": SYSTEM_CA_BUNDLE,
            "NODE_EXTRA_CA_CERTS": environment.PROXY_CA_PATH,
        }

    async def delete_secret(self, *, vm: str, placeholder: Placeholder) -> None:
        return

    def get_public_certificate(self) -> bytes:
        """mitmproxy writes the key and the certificate into one file and the
        certificate alone into another. Only the second may reach a box."""
        path = get_settings().secrets_proxy_ca_file
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
