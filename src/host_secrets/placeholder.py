import hashlib
import secrets
import uuid

# A placeholder names the box and the service it stands in for, so the edge
# can find the one entry to check it against without a lookup table.
PREFIX = "drk"


def mint(host_id: uuid.UUID, service: str) -> tuple[str, str]:
    """Return a new placeholder and the digest the entry keeps for it."""
    secret = secrets.token_urlsafe(32)
    return f"{PREFIX}.{host_id.hex}.{service}.{secret}", digest(secret)


def parse(placeholder: str) -> tuple[uuid.UUID, str, str]:
    prefix, host_hex, service, secret = placeholder.split(".")
    if prefix != PREFIX:
        raise ValueError("not a placeholder")
    return uuid.UUID(host_hex), service, secret


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
