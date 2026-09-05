import os

import pytest

import conftest
from core.settings import Settings, get_settings
from networking.tailscale_settings import TailscaleSettings


def _base_env() -> dict[str, str]:
    return {
        "DATABASE_URL": "sqlite+aiosqlite:///./.drukbox-test.db",
        "SECRETS_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        "SERVICE_TOKENS": "tok",
    }


def _settings_with(monkeypatch: pytest.MonkeyPatch, env: dict[str, str | None]) -> Settings:
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return Settings()  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("blank", ["", " , "])
def test_service_tokens_must_contain_a_token(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    # An empty or comma-only SERVICE_TOKENS parses to an empty tuple, which would
    # start a service that rejects everyone; it must fail fast at construction.
    env: dict[str, str | None] = {**_base_env(), "SERVICE_TOKENS": blank}
    with pytest.raises(ValueError, match="SERVICE_TOKENS"):
        _settings_with(monkeypatch, env)


def test_secrets_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str | None] = {**_base_env(), "SECRETS_KEY": None}

    with pytest.raises(ValueError, match="SECRETS_KEY"):
        _settings_with(monkeypatch, env)


def test_secrets_key_accepts_a_rotation_list(monkeypatch: pytest.MonkeyPatch) -> None:
    first = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    second = "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="
    env: dict[str, str | None] = {
        **_base_env(),
        "SECRETS_KEY": f" {first}, {second} ",
    }

    settings = _settings_with(monkeypatch, env)

    assert settings.secrets_key.get_secret_value() == f"{first},{second}"
    assert first not in repr(settings)


def test_secrets_key_rejects_invalid_key_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_key = "not-a-secret-key"
    env: dict[str, str | None] = {**_base_env(), "SECRETS_KEY": invalid_key}

    with pytest.raises(ValueError) as error:
        _settings_with(monkeypatch, env)

    assert "base64-encoded" in str(error.value)
    assert invalid_key not in str(error.value)


def test_tailscale_disabled_by_default_with_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str | None] = {
        **_base_env(),
        "TAILSCALE_ENABLED": None,
        "TAILSCALE_TAILNET": None,
        "TAILSCALE_AUTH_TAGS": None,
        "TAILSCALE_OAUTH_CLIENT_ID": None,
        "TAILSCALE_OAUTH_CLIENT_SECRET": None,
    }
    settings = _settings_with(monkeypatch, env)
    assert settings.tailscale_enabled is False


def test_tailscale_settings_requires_all_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = ("TAILSCALE_AUTH_TAGS", "TAILSCALE_OAUTH_CLIENT_ID", "TAILSCALE_OAUTH_CLIENT_SECRET")
    for key in missing:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TAILSCALE_TAILNET", "example.ts.net")

    with pytest.raises(ValueError) as excinfo:
        TailscaleSettings()  # pyright: ignore[reportCallIssue]
    message = str(excinfo.value)
    assert "TAILSCALE_AUTH_TAGS" in message
    assert "TAILSCALE_OAUTH_CLIENT_ID" in message
    assert "TAILSCALE_OAUTH_CLIENT_SECRET" in message


def test_tailscale_settings_with_all_credentials_constructs_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAILSCALE_TAILNET", "example.ts.net")
    monkeypatch.setenv("TAILSCALE_AUTH_TAGS", "tag:sandbox")
    monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_SECRET", "client-secret")

    ts = TailscaleSettings()  # pyright: ignore[reportCallIssue]
    assert ts.tailnet == "example.ts.net"
    assert ts.auth_tags == ("tag:sandbox",)


def test_tailscale_disabled_ignores_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str | None] = {
        **_base_env(),
        "TAILSCALE_ENABLED": "false",
        "TAILSCALE_TAILNET": None,
    }
    settings = _settings_with(monkeypatch, env)
    assert settings.tailscale_enabled is False


@pytest.mark.parametrize(
    "key",
    [
        "DEVICE_DISCOVERY_TIMEOUT_SECONDS",
        "IDEMPOTENCY_KEY_TTL_HOURS",
        "PROVISIONING_GRACE_SECONDS",
        "POOL_SIZE",
        "POOL_HOST_MAX_AGE_HOURS",
        "POOL_MAX_CREATES_PER_TICK",
        "TEMPLATE_BUILD_TIMEOUT",
        "TEMPLATE_FAILED_RETENTION",
        "TEMPLATE_UNUSED_TTL",
    ],
)
def test_numeric_settings_reject_negative_values(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    env: dict[str, str | None] = {**_base_env(), key: "-1"}
    with pytest.raises(ValueError, match=key):
        _settings_with(monkeypatch, env)


def test_pool_size_seeds_the_default_providers_target(monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str | None] = {
        **_base_env(),
        "DEFAULT_HOST_PROVIDER": None,
        "POOL_SIZE": "2",
        "POOL_SIZES": None,
    }
    settings = _settings_with(monkeypatch, env)
    assert settings.get_pool_targets() == {"exe": 2}


def test_pool_sizes_overrides_the_alias_for_the_same_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env: dict[str, str | None] = {
        **_base_env(),
        "DEFAULT_HOST_PROVIDER": None,
        "POOL_SIZE": "5",
        "POOL_SIZES": '{"exe": 2, "hetzner": 1}',
    }
    settings = _settings_with(monkeypatch, env)
    assert settings.get_pool_targets() == {"exe": 2, "hetzner": 1}


def test_pool_targets_omit_zeroed_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit zero in POOL_SIZES disables that provider's pool even when
    # the POOL_SIZE alias would seed it.
    env: dict[str, str | None] = {
        **_base_env(),
        "DEFAULT_HOST_PROVIDER": None,
        "POOL_SIZE": "5",
        "POOL_SIZES": '{"exe": 0}',
    }
    settings = _settings_with(monkeypatch, env)
    assert settings.get_pool_targets() == {}


def test_pool_sizes_rejects_negative_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str | None] = {**_base_env(), "POOL_SIZES": '{"exe": -1}'}
    with pytest.raises(ValueError, match="POOL_SIZES"):
        _settings_with(monkeypatch, env)


def test_load_test_env_overrides_ambient_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAILSCALE_ENABLED", "false")
    conftest.load_test_env()
    assert os.environ["TAILSCALE_ENABLED"] == "true"


@pytest.mark.parametrize("host", ["ghcr.io", "docker.io", "registry.example:5000"])
def test_registry_access_does_not_require_templates(monkeypatch, host):
    settings = _settings_with(
        monkeypatch,
        {
            **_base_env(),
            "REGISTRY_HOST": host,
            "REGISTRY_USERNAME": "builder",
            "REGISTRY_PASSWORD": "private-token",
            "TEMPLATE_REPOSITORY": "",
        },
    )
    assert settings.registry_host == host
    assert settings.template_repository == ""
    assert settings.registry_password.get_secret_value() == "private-token"
    assert "private-token" not in repr(settings)


@pytest.mark.parametrize("repository", ["acme/templates", "org/team/templates"])
def test_template_destination_uses_registry_access(monkeypatch, repository):
    settings = _settings_with(
        monkeypatch,
        {
            **_base_env(),
            "REGISTRY_HOST": "ghcr.io",
            "REGISTRY_USERNAME": "builder",
            "REGISTRY_PASSWORD": "private-token",
            "TEMPLATE_REPOSITORY": repository,
        },
    )
    assert settings.template_repository == repository


@pytest.mark.parametrize("host", ["https://ghcr.io", "ghcr.io/acme", "ghcr.io@evil.example"])
def test_registry_host_rejects_url_and_path(monkeypatch, host):
    with pytest.raises(ValueError, match="REGISTRY_HOST"):
        _settings_with(
            monkeypatch,
            {
                **_base_env(),
                "REGISTRY_HOST": host,
                "REGISTRY_USERNAME": "builder",
                "REGISTRY_PASSWORD": "private-token",
            },
        )


@pytest.mark.parametrize(
    "repository",
    [
        "https://ghcr.io/acme/templates",
        "acme/templates:latest",
        "acme/templates@sha256:abc",
        "acme/",
    ],
)
def test_template_repository_rejects_url_tag_or_digest(monkeypatch, repository):
    with pytest.raises(ValueError, match="TEMPLATE_REPOSITORY"):
        _settings_with(
            monkeypatch,
            {
                **_base_env(),
                "REGISTRY_HOST": "ghcr.io",
                "REGISTRY_USERNAME": "builder",
                "REGISTRY_PASSWORD": "private-token",
                "TEMPLATE_REPOSITORY": repository,
            },
        )


def test_partial_registry_names_missing_setting_without_secret(monkeypatch):
    with pytest.raises(ValueError) as error:
        _settings_with(
            monkeypatch,
            {
                **_base_env(),
                "REGISTRY_HOST": "ghcr.io",
                "REGISTRY_USERNAME": "",
                "REGISTRY_PASSWORD": "private-token",
            },
        )
    assert "REGISTRY_USERNAME" in str(error.value)
    assert "private-token" not in str(error.value)


def test_template_destination_requires_registry_access(monkeypatch):
    with pytest.raises(ValueError, match="REGISTRY_HOST"):
        _settings_with(
            monkeypatch,
            {
                **_base_env(),
                "REGISTRY_HOST": "",
                "REGISTRY_USERNAME": "",
                "REGISTRY_PASSWORD": "",
                "TEMPLATE_REPOSITORY": "acme/templates",
            },
        )
