from typing import Any, TypedDict

BEARER_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
SERVICE_FIELDS = frozenset({"host", "credential_header", "credential_prefix", "credential_var"})


class Service(TypedDict):
    host: str
    credential_header: str
    credential_prefix: str
    credential_var: str


def bearer(host: str, credential_var: str) -> Service:
    return {
        "host": host,
        "credential_header": BEARER_HEADER,
        "credential_prefix": BEARER_PREFIX,
        "credential_var": credential_var,
    }


CATALOG: dict[str, Service] = {
    "anthropic": bearer("api.anthropic.com", "ANTHROPIC_AUTH_TOKEN"),
    "github": bearer("api.github.com", "GH_TOKEN"),
    "openai": bearer("api.openai.com", "OPENAI_API_KEY"),
}


def service(name: str, entry: dict[str, Any]) -> Service:
    """The service an entry reaches: its host, header, and the variable a client reads."""
    fields = entry if "host" in entry else CATALOG[name]
    return {
        "host": fields["host"],
        "credential_header": fields["credential_header"],
        "credential_prefix": fields["credential_prefix"],
        "credential_var": fields["credential_var"],
    }
