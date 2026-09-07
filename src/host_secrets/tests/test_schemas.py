import pytest
from pydantic import ValidationError

from host_secrets.schemas import SecretEntry


def _issuer(**overrides: object) -> dict[str, object]:
    return {
        "url": "https://mint.example.test/token",
        "headers": {"Authorization": "Bearer secret"},
        "refresh": "50m",
        **overrides,
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
        {"host": "api.acme.test", "auth_variable": "ACME_TOKEN", "value": "static-secret"}
    )

    assert registration.to_storage() == {
        "host": "api.acme.test",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "auth_variable": "ACME_TOKEN",
        "value": "static-secret",
    }


def test_custom_entry_can_override_the_auth_shape() -> None:
    registration = SecretEntry.model_validate(
        {
            "host": "api.acme.test",
            "auth_header": "x-api-key",
            "auth_prefix": "",
            "auth_variable": "ACME_TOKEN",
            "value": "static-secret",
        }
    )

    assert registration.to_storage() == {
        "host": "api.acme.test",
        "auth_header": "x-api-key",
        "auth_prefix": "",
        "auth_variable": "ACME_TOKEN",
        "value": "static-secret",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"value": "one", "issuer": _issuer()},
        {"host": "api.acme.test", "value": "one"},
        {"auth_variable": "ACME_TOKEN", "value": "one"},
        {"auth_prefix": "", "value": "one"},
        {"placeholder": "managed", "value": "one"},
        {"host": "api.acme.test", "auth_variable": "not a variable", "value": "one"},
    ],
)
def test_registration_rejects_ambiguous_or_incomplete_shapes(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SecretEntry.model_validate(payload)


@pytest.mark.parametrize(
    "url",
    [
        "https://mint.example.test/token",
        "http://127.0.0.1:8001/api/mint/grant/github",
        "http://web:8000/api/mint/grant/github",
    ],
)
def test_issuer_accepts_http_inside_the_deployment_and_https_anywhere(url: str) -> None:
    entry = SecretEntry.model_validate({"issuer": _issuer(url=url)})

    assert entry.issuer and str(entry.issuer.url) == url


@pytest.mark.parametrize(
    "issuer",
    [
        _issuer(url="ftp://mint.example.test/token"),
        _issuer(url="https://user:password@mint.example.test/token"),
        _issuer(url="https://mint.example.test/token#credential"),
        _issuer(headers={"Authorization": "Bearer secret\n"}),
        _issuer(refresh="0m"),
        _issuer(refresh="50minutes"),
        _issuer(headers={}),
    ],
)
def test_issuer_rejects_unsafe_or_invalid_recipes(issuer: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SecretEntry.model_validate({"issuer": issuer})
