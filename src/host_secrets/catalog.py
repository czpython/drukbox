from typing import TypedDict

BEARER_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "


class Service(TypedDict):
    host: str
    credential_header: str
    credential_prefix: str
    credential_var: str
    # Empty when the client has no base URL variable, so endpoint
    # substitution cannot reach the service.
    endpoint_var: str


def bearer(host: str, credential_var: str, endpoint_var: str = "") -> Service:
    return {
        "host": host,
        "credential_header": BEARER_HEADER,
        "credential_prefix": BEARER_PREFIX,
        "credential_var": credential_var,
        "endpoint_var": endpoint_var,
    }


CATALOG: dict[str, Service] = {
    "anthropic": bearer("api.anthropic.com", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "github": bearer("api.github.com", "GH_TOKEN"),
    "openai": bearer("api.openai.com", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
}
