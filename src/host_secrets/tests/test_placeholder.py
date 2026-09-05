import uuid

import pytest

from host_secrets.placeholder import digest, mint, parse


def test_placeholder_names_its_host_and_service() -> None:
    host_id = uuid.uuid4()

    placeholder, stored = mint(host_id, "github")
    parsed_host, service, secret = parse(placeholder)

    assert (parsed_host, service) == (host_id, "github")
    assert digest(secret) == stored
    assert placeholder.startswith(f"drk.{host_id.hex}.github.")


def test_two_placeholders_for_one_service_differ() -> None:
    host_id = uuid.uuid4()
    assert mint(host_id, "github")[0] != mint(host_id, "github")[0]


@pytest.mark.parametrize("value", ["", "sk-ant-oat01-real", "drk.nothex.github.x", "abc.def"])
def test_parse_rejects_anything_that_is_not_a_placeholder(value: str) -> None:
    with pytest.raises(ValueError):
        parse(value)
