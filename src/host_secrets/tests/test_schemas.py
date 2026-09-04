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


def test_custom_entry_is_self_describing() -> None:
    registration = SecretRegistration.model_validate(
        {
            "host": "api.acme.test",
            "auth_var": "ACME_TOKEN",
            "placeholder": "acme-proxy-managed",
            "value": "static-secret",
        }
    )

    assert registration.to_storage() == {
        "host": "api.acme.test",
        "auth_var": "ACME_TOKEN",
        "placeholder": "acme-proxy-managed",
        "value": "static-secret",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"value": "one", "source": _source()},
        {"host": "api.acme.test", "value": "one"},
        {"auth_var": "ACME_TOKEN", "value": "one"},
        {"placeholder": "managed", "value": "one"},
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
