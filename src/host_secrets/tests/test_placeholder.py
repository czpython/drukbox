import uuid

import pytest

from host_secrets.placeholder import Placeholder, issue_placeholders
from hosts.models import Host


def test_a_read_placeholder_names_its_host_and_service_and_matches_its_fingerprint() -> None:
    host_id = uuid.uuid4()
    minted = Placeholder.mint(host_id, "github")

    read = Placeholder.read(f"Bearer {minted}")

    assert (read.host_id, read.service) == (host_id, "github")
    assert read.matches(minted.fingerprint)
    assert str(minted).startswith(f"drk.{host_id.hex}.github.")


def test_two_placeholders_for_one_service_differ() -> None:
    host_id = uuid.uuid4()
    assert Placeholder.mint(host_id, "github") != Placeholder.mint(host_id, "github")


def test_a_wrong_secret_does_not_match() -> None:
    minted = Placeholder.mint(uuid.uuid4(), "github")
    assert not minted._replace(secret="other").matches(minted.fingerprint)


@pytest.mark.parametrize("value", ["", "sk-ant-oat01-real", "drk.nothex.github.x", "abc.def"])
def test_anything_that_is_not_a_placeholder_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        Placeholder.read(value)


def test_issue_placeholders_mints_a_placeholder_per_secret_and_keeps_only_its_fingerprint() -> None:
    host = Host(
        id=uuid.uuid4(),
        name="sb-one",
        image="sandbox:latest",
        secrets={
            "anthropic": {"value": "sk-ant-real"},
            "acme": {
                "host": "api.acme.test",
                "credential_header": "x-api-key",
                "credential_prefix": "",
                "credential_var": "ACME_TOKEN",
                "endpoint_var": "ACME_BASE_URL",
                "base_path": "/v2",
                "value": "ak_live",
            },
        },
    )

    environment = issue_placeholders(host, "https://secrets.example")

    assert environment["ANTHROPIC_BASE_URL"] == "https://secrets.example/api.anthropic.com"
    assert environment["ACME_BASE_URL"] == "https://secrets.example/api.acme.test/v2"
    for name, variable in (("anthropic", "ANTHROPIC_AUTH_TOKEN"), ("acme", "ACME_TOKEN")):
        placeholder = Placeholder.read(environment[variable])
        assert (placeholder.host_id, placeholder.service) == (host.id, name)
        assert placeholder.matches(host.secrets[name]["placeholder_fingerprint"])
    assert host.secrets["anthropic"]["value"] == "sk-ant-real"
    assert "sk-ant-real" not in environment.values()


def test_issue_placeholders_gives_nothing_for_a_host_without_secrets() -> None:
    host = Host(id=uuid.uuid4(), name="sb-one", image="sandbox:latest", secrets={})

    assert issue_placeholders(host, "https://secrets.example") == {}
