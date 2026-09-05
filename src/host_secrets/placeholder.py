import hashlib
import hmac
import secrets
import uuid
from typing import NamedTuple, Self

from host_secrets import catalog
from hosts.models import Host

# A placeholder names its box and its service. The exchange reads both and
# checks one entry, so no lookup table exists.
_PREFIX = "drk"


class Placeholder(NamedTuple):
    host_id: uuid.UUID
    service: str
    secret: str

    @classmethod
    def mint(cls, host_id: uuid.UUID, service: str) -> Self:
        return cls(host_id, service, secrets.token_urlsafe(32))

    @classmethod
    def read(cls, authorization: str) -> Self:
        """A placeholder from an Authorization header or an environment line."""
        prefix, host_hex, service, secret = authorization.removeprefix("Bearer ").split(".")
        if prefix != _PREFIX:
            raise ValueError("not a placeholder")
        return cls(uuid.UUID(host_hex), service, secret)

    def __str__(self) -> str:
        return f"{_PREFIX}.{self.host_id.hex}.{self.service}.{self.secret}"

    @property
    def fingerprint(self) -> str:
        """What the entry stores in place of the secret."""
        return hashlib.sha256(self.secret.encode()).hexdigest()

    def matches(self, fingerprint: str) -> bool:
        return hmac.compare_digest(self.fingerprint, fingerprint)

    def environment(self, service: dict[str, str], exchange_url: str) -> dict[str, str]:
        """What the box needs to reach ``service`` through the exchange with this placeholder."""
        return {
            service["credential_var"]: str(self),
            service["endpoint_var"]: f"{exchange_url}/{service['host']}{service['base_path']}",
        }


def issue_placeholders(host: Host, exchange_url: str) -> dict[str, str]:
    """Mint one placeholder per secret, keep its fingerprint, and return the box's environment."""
    environment: dict[str, str] = {}
    for name, entry in host.secrets.items():
        placeholder = Placeholder.mint(host.id, name)
        host.secrets[name] = {**entry, "placeholder_fingerprint": placeholder.fingerprint}
        environment.update(placeholder.environment(catalog.service(name, entry), exchange_url))
    return environment
