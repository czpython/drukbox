import base64
from typing import Any, NamedTuple

BEARER_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
# What a custom entry stores about its service, next to the value or the issuer.
SERVICE_FIELDS = frozenset({"host", "credential_header", "credential_prefix", "credential_var"})


class Upstream(NamedTuple):
    """One host a service reaches, and how the credential goes on the wire to it."""

    host: str
    header: str = BEARER_HEADER
    prefix: str = BEARER_PREFIX
    # Basic authentication carries the credential as this user's password.
    basic_user: str = ""

    def credential(self, value: str) -> str:
        if self.basic_user:
            return "Basic " + base64.b64encode(f"{self.basic_user}:{value}".encode()).decode()
        return f"{self.prefix}{value}"


class Service(NamedTuple):
    """The variable a client reads the credential from, and the hosts it reaches."""

    credential_var: str
    upstreams: tuple[Upstream, ...]


CATALOG: dict[str, Service] = {
    "anthropic": Service("ANTHROPIC_AUTH_TOKEN", (Upstream("api.anthropic.com"),)),
    # gh sends a bearer to the API and to release asset uploads. git's smart
    # HTTP refuses a bearer and takes Basic with x-access-token as the user.
    "github": Service(
        "GH_TOKEN",
        (
            Upstream("api.github.com"),
            Upstream("uploads.github.com"),
            Upstream("github.com", basic_user="x-access-token"),
        ),
    ),
    "openai": Service("OPENAI_API_KEY", (Upstream("api.openai.com"),)),
}


def service(name: str, entry: dict[str, Any]) -> Service:
    """The service an entry reaches. A custom entry names one host of its own."""
    if "host" in entry:
        return Service(
            entry["credential_var"],
            (Upstream(entry["host"], entry["credential_header"], entry["credential_prefix"]),),
        )
    return CATALOG[name]
