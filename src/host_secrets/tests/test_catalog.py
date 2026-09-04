import pytest

from host_secrets.catalog import SECRET_CATALOG, resolve_secret_config
from host_secrets.exceptions import UnknownSecretServiceError


def test_catalog_defines_the_initial_built_in_services() -> None:
    assert SECRET_CATALOG == {
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


def test_custom_entry_bypasses_the_catalog() -> None:
    assert resolve_secret_config(
        "acme",
        {
            "host": "api.acme.test",
            "auth_var": "ACME_TOKEN",
            "placeholder": "acme-proxy-managed",
            "value": "secret",
        },
    ) == {
        "host": "api.acme.test",
        "auth_var": "ACME_TOKEN",
        "base_url_var": "",
        "placeholder": "acme-proxy-managed",
    }


def test_unknown_built_in_service_is_rejected() -> None:
    with pytest.raises(UnknownSecretServiceError, match="unknown secret service 'acme'"):
        resolve_secret_config("acme", {"value": "secret"})
