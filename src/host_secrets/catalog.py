from typing import TypedDict

from host_secrets.exceptions import UnknownSecretServiceError
from host_secrets.schemas import SecretEntry


class SecretConfig(TypedDict):
    host: str
    auth_var: str
    base_url_var: str
    placeholder: str


SECRET_CATALOG: dict[str, SecretConfig] = {
    "anthropic": {
        "host": "api.anthropic.com",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "base_url_var": "ANTHROPIC_BASE_URL",
        "placeholder": "sk-ant-proxy-managed",
    },
    "dockerhub": {
        "host": "registry-1.docker.io",
        "auth_var": "DOCKERHUB_TOKEN",
        "base_url_var": "",
        "placeholder": "dockerhub-proxy-managed",
    },
    "github": {
        "host": "api.github.com",
        "auth_var": "GH_TOKEN",
        "base_url_var": "",
        "placeholder": "github-proxy-managed",
    },
    "openai": {
        "host": "api.openai.com",
        "auth_var": "OPENAI_API_KEY",
        "base_url_var": "OPENAI_BASE_URL",
        "placeholder": "sk-proxy-managed",
    },
}


def resolve_secret_config(name: str, entry: SecretEntry) -> SecretConfig:
    if "host" in entry:
        return {
            "host": entry["host"],
            "auth_var": entry["auth_var"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
            "base_url_var": "",
            "placeholder": entry["placeholder"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
        }

    try:
        return SECRET_CATALOG[name].copy()
    except KeyError as exc:
        raise UnknownSecretServiceError(f"unknown secret service {name!r}") from exc
