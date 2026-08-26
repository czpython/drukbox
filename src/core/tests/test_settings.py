import os

import pytest

import conftest
from core.settings import Settings, get_settings
from networking.tailscale_settings import TailscaleSettings


def _base_env() -> dict[str, str]:
    return {
        "DATABASE_URL": "sqlite+aiosqlite:///./.drukbox-test.db",
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


def test_template_maintenance_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with(
        monkeypatch,
        {
            **_base_env(),
            "TEMPLATE_BUILD_TIMEOUT": None,
            "TEMPLATE_FAILED_RETENTION": None,
            "TEMPLATE_UNUSED_TTL": None,
        },
    )

    assert settings.template_build_timeout == 3600
    assert settings.template_failed_retention == 86400
    assert settings.template_unused_ttl == 1209600


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
