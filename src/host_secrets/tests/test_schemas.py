import pytest
from pydantic import ValidationError

from host_secrets.schemas import SecretRegistration


def _source(
    *,
    url: str = "https://mint.example.test/token",
    headers: dict[str, str] | None = None,
    refresh: str = "50m",
) -> dict[str, object]:
    return {
        "url": url,
        "headers": headers if headers is not None else {"Authorization": "Bearer secret"},
        "refresh": refresh,
    }


def test_static_built_in_entry_has_only_the_value() -> None:
    registration = SecretRegistration.model_validate({"value": "static-secret"})

    assert registration.to_storage() == {"value": "static-secret"}
    assert "static-secret" not in repr(registration)


def test_refreshable_entry_preserves_the_readable_recipe() -> None:
    registration = SecretRegistration.model_validate(
        {
            "source": {
                "url": "https://mint.example.test/boxes/box-1/token?audience=github",
                "headers": {"Authorization": "Bearer fetch-secret"},
                "refresh": "50m",
            }
        }
    )

    assert registration.to_storage() == {
        "source": {
            "url": "https://mint.example.test/boxes/box-1/token?audience=github",
            "headers": {"Authorization": "Bearer fetch-secret"},
            "refresh": "50m",
        }
    }
    assert "fetch-secret" not in repr(registration)


def test_custom_entry_stores_the_whole_service_with_bearer_defaults() -> None:
    registration = SecretRegistration.model_validate(
        {"host": "api.acme.test", "credential_var": "ACME_TOKEN", "value": "static-secret"}
    )

    assert registration.to_storage() == {
        "host": "api.acme.test",
        "credential_header": "Authorization",
        "credential_prefix": "Bearer ",
        "credential_var": "ACME_TOKEN",
        "endpoint_var": "",
        "base_path": "",
        "value": "static-secret",
    }


def test_custom_entry_can_override_the_auth_shape() -> None:
    registration = SecretRegistration.model_validate(
        {
            "host": "api.acme.test",
            "credential_header": "x-api-key",
            "credential_prefix": "",
            "credential_var": "ACME_TOKEN",
            "endpoint_var": "ACME_BASE_URL",
            "value": "static-secret",
        }
    )

    assert registration.to_storage() == {
        "host": "api.acme.test",
        "credential_header": "x-api-key",
        "credential_prefix": "",
        "credential_var": "ACME_TOKEN",
        "endpoint_var": "ACME_BASE_URL",
        "base_path": "",
        "value": "static-secret",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"value": "one", "source": _source()},
        {"host": "api.acme.test", "value": "one"},
        {"credential_var": "ACME_TOKEN", "value": "one"},
        {"credential_prefix": "", "value": "one"},
        {"endpoint_var": "", "value": "one"},
        {"placeholder": "managed", "value": "one"},
        {"host": "api.acme.test", "credential_var": "not a variable", "value": "one"},
    ],
)
def test_registration_rejects_ambiguous_or_incomplete_shapes(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SecretRegistration.model_validate(payload)


@pytest.mark.parametrize(
    "source",
    [
        _source(url="http://mint.example.test/token"),
        _source(url="https://user:password@mint.example.test/token"),
        _source(url="https://mint.example.test/token#credential"),
        _source(refresh="0m"),
        _source(refresh="50minutes"),
        _source(headers={}),
    ],
)
def test_source_rejects_unsafe_or_invalid_recipes(source: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SecretRegistration.model_validate({"source": source})
