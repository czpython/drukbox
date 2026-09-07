import base64
from typing import Any, NamedTuple

BEARER_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
SERVICE_FIELDS = frozenset({"host", "auth_header", "auth_prefix", "auth_variable"})


class Upstream(NamedTuple):
    host: str
    auth_header: str = BEARER_HEADER
    auth_prefix: str = BEARER_PREFIX
    # Basic carries the credential as this user's password.
    basic_user: str = ""

    def credential(self, value: str) -> str:
        if self.basic_user:
            return "Basic " + base64.b64encode(f"{self.basic_user}:{value}".encode()).decode()
        return f"{self.auth_prefix}{value}"


class Service(NamedTuple):
    auth_variable: str
    upstreams: tuple[Upstream, ...]


CATALOG: dict[str, Service] = {
    "anthropic": Service("ANTHROPIC_AUTH_TOKEN", (Upstream("api.anthropic.com"),)),
    # git's smart HTTP refuses a bearer. It takes Basic with x-access-token as the user.
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
    if "host" in entry:
        return Service(
            entry["auth_variable"],
            (Upstream(entry["host"], entry["auth_header"], entry["auth_prefix"]),),
        )
    return CATALOG[name]
