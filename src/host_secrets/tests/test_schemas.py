import pytest
from pydantic import ValidationError

from host_secrets.schemas import SecretEntry


def _issuer(
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
    registration = SecretEntry.model_validate({"value": "static-secret"})

    assert registration.to_storage() == {"value": "static-secret"}
    assert "static-secret" not in repr(registration)


def test_refreshable_entry_preserves_the_readable_recipe() -> None:
    registration = SecretEntry.model_validate(
        {
            "issuer": {
                "url": "https://mint.example.test/boxes/box-1/token?audience=github",
                "headers": {"Authorization": "Bearer fetch-secret"},
                "refresh": "50m",
            }
        }
    )

    assert registration.to_storage() == {
        "issuer": {
            "url": "https://mint.example.test/boxes/box-1/token?audience=github",
            "headers": {"Authorization": "Bearer fetch-secret"},
            "refresh": "50m",
        }
    }
    assert "fetch-secret" not in repr(registration)


def test_custom_entry_stores_the_whole_service_with_bearer_defaults() -> None:
    registration = SecretEntry.model_validate(
        {"host": "api.acme.test", "credential_var": "ACME_TOKEN", "value": "static-secret"}
    )

    assert registration.to_storage() == {
        "host": "api.acme.test",
        "credential_header": "Authorization",
        "credential_prefix": "Bearer ",
        "credential_var": "ACME_TOKEN",
        "value": "static-secret",
    }


def test_custom_entry_can_override_the_auth_shape() -> None:
    registration = SecretEntry.model_validate(
        {
            "host": "api.acme.test",
            "credential_header": "x-api-key",
            "credential_prefix": "",
            "credential_var": "ACME_TOKEN",
            "value": "static-secret",
        }
    )

    assert registration.to_storage() == {
        "host": "api.acme.test",
        "credential_header": "x-api-key",
        "credential_prefix": "",
        "credential_var": "ACME_TOKEN",
        "value": "static-secret",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"value": "one", "issuer": _issuer()},
        {"host": "api.acme.test", "value": "one"},
        {"credential_var": "ACME_TOKEN", "value": "one"},
        {"credential_prefix": "", "value": "one"},
        {"placeholder": "managed", "value": "one"},
        {"host": "api.acme.test", "credential_var": "not a variable", "value": "one"},
    ],
)
def test_registration_rejects_ambiguous_or_incomplete_shapes(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SecretEntry.model_validate(payload)


@pytest.mark.parametrize(
    "issuer",
    [
        _issuer(url="http://mint.example.test/token"),
        _issuer(url="https://user:password@mint.example.test/token"),
        _issuer(url="https://mint.example.test/token#credential"),
        _issuer(refresh="0m"),
        _issuer(refresh="50minutes"),
        _issuer(headers={}),
    ],
)
def test_issuer_rejects_unsafe_or_invalid_recipes(issuer: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SecretEntry.model_validate({"issuer": issuer})
