import uuid

import pytest

from host_secrets.placeholder import Placeholder


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
