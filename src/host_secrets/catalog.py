from typing import Any, TypedDict

BEARER_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
SERVICE_FIELDS = frozenset(
    {
        "host",
        "credential_header",
        "credential_prefix",
        "credential_var",
        "endpoint_var",
        "base_path",
    }
)


class Service(TypedDict):
    host: str
    credential_header: str
    credential_prefix: str
    credential_var: str
    # Empty when the client has no base URL variable, so endpoint
    # substitution cannot reach the service.
    endpoint_var: str
    # What the client expects after the host in its base URL, "/v1" for OpenAI.
    base_path: str


def bearer(host: str, credential_var: str, endpoint_var: str = "", base_path: str = "") -> Service:
    return {
        "host": host,
        "credential_header": BEARER_HEADER,
        "credential_prefix": BEARER_PREFIX,
        "credential_var": credential_var,
        "endpoint_var": endpoint_var,
        "base_path": base_path,
    }


CATALOG: dict[str, Service] = {
    "anthropic": bearer("api.anthropic.com", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "github": bearer("api.github.com", "GH_TOKEN"),
    "openai": bearer("api.openai.com", "OPENAI_API_KEY", "OPENAI_BASE_URL", "/v1"),
}


def service(name: str, entry: dict[str, Any]) -> Service:
    """The service an entry reaches: host, header, variables, base path."""
    fields = entry if "host" in entry else CATALOG[name]
    return {
        "host": fields["host"],
        "credential_header": fields["credential_header"],
        "credential_prefix": fields["credential_prefix"],
        "credential_var": fields["credential_var"],
        "endpoint_var": fields["endpoint_var"],
        "base_path": fields["base_path"],
    }
