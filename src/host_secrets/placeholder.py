import hashlib
import hmac
import secrets
import uuid
from typing import NamedTuple, Self

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
