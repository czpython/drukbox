from typing import Any, TypedDict

BEARER_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
SERVICE_FIELDS = frozenset(
    {
        "host",
        "auth_header",
        "auth_prefix",
        "auth_variable",
        "endpoint_var",
        "base_path",
    }
)


class Service(TypedDict):
    host: str
    auth_header: str
    auth_prefix: str
    auth_variable: str
    # Empty when the client has no base URL variable.
    endpoint_var: str
    # "/v1" for OpenAI.
    base_path: str


def bearer(host: str, auth_variable: str, endpoint_var: str = "", base_path: str = "") -> Service:
    return {
        "host": host,
        "auth_header": BEARER_HEADER,
        "auth_prefix": BEARER_PREFIX,
        "auth_variable": auth_variable,
        "endpoint_var": endpoint_var,
        "base_path": base_path,
    }


CATALOG: dict[str, Service] = {
    "anthropic": bearer("api.anthropic.com", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "github": bearer("api.github.com", "GH_TOKEN"),
    "openai": bearer("api.openai.com", "OPENAI_API_KEY", "OPENAI_BASE_URL", "/v1"),
}


def service(name: str, entry: dict[str, Any]) -> Service:
    fields = entry if "host" in entry else CATALOG[name]
    return {
        "host": fields["host"],
        "auth_header": fields["auth_header"],
        "auth_prefix": fields["auth_prefix"],
        "auth_variable": fields["auth_variable"],
        "endpoint_var": fields["endpoint_var"],
        "base_path": fields["base_path"],
    }
