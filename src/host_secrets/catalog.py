from typing import Any, TypedDict

BEARER_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
SERVICE_FIELDS = frozenset({"host", "auth_header", "auth_prefix", "auth_variable"})


class Service(TypedDict):
    host: str
    auth_header: str
    auth_prefix: str
    auth_variable: str


def bearer(host: str, auth_variable: str) -> Service:
    return {
        "host": host,
        "auth_header": BEARER_HEADER,
        "auth_prefix": BEARER_PREFIX,
        "auth_variable": auth_variable,
    }


CATALOG: dict[str, Service] = {
    "anthropic": bearer("api.anthropic.com", "ANTHROPIC_AUTH_TOKEN"),
    "github": bearer("api.github.com", "GH_TOKEN"),
    "openai": bearer("api.openai.com", "OPENAI_API_KEY"),
}


def service(name: str, entry: dict[str, Any]) -> Service:
    fields = entry if "host" in entry else CATALOG[name]
    return {
        "host": fields["host"],
        "auth_header": fields["auth_header"],
        "auth_prefix": fields["auth_prefix"],
        "auth_variable": fields["auth_variable"],
    }
