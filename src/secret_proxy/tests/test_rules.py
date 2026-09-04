import asyncio

import pytest

from secret_proxy.exceptions import SecretProxyRejectedError
from secret_proxy.rules import SecretRules


@pytest.mark.asyncio
async def test_put_replaces_one_rule_without_exposing_values_in_the_listing() -> None:
    rules = SecretRules(allow_private_upstreams=True)

    await asyncio.gather(
        rules.put(
            vm="box-one",
            name="openai",
            host="127.0.0.1:8443",
            placeholder="first-placeholder",
            value="first-secret",
        ),
        rules.put(
            vm="box-one",
            name="openai",
            host="127.0.0.1:8443",
            placeholder="second-placeholder",
            value="second-secret",
        ),
    )

    assert rules.names(vm="box-one") == ["openai"]
    assert rules.for_host(vm="box-one", host="127.0.0.1:8443")[0]["value"] in {
        "first-secret",
        "second-secret",
    }


@pytest.mark.asyncio
async def test_rules_reject_private_upstreams_by_default() -> None:
    rules = SecretRules()

    with pytest.raises(SecretProxyRejectedError, match="private or reserved"):
        await rules.put(
            vm="box-one",
            name="openai",
            host="127.0.0.1",
            placeholder="placeholder",
            value="secret",
        )


@pytest.mark.asyncio
async def test_rules_are_box_scoped() -> None:
    rules = SecretRules(allow_private_upstreams=True)
    await rules.put(
        vm="box-one",
        name="openai",
        host="127.0.0.1:8443",
        placeholder="placeholder",
        value="secret",
    )

    assert not rules.for_host(vm="box-two", host="127.0.0.1:8443")
    assert not rules.for_host(vm="box-one", host="127.0.0.1:9443")


@pytest.mark.asyncio
async def test_rules_keep_opaque_values_for_body_substitution() -> None:
    rules = SecretRules(allow_private_upstreams=True)
    await rules.put(
        vm="box-one",
        name="custom",
        host="127.0.0.1:8443",
        placeholder="placeholder",
        value="first line\nsecond line",
    )

    assert rules.for_host(vm="box-one", host="127.0.0.1:8443")[0]["value"] == (
        "first line\nsecond line"
    )


@pytest.mark.asyncio
async def test_delete_removes_the_last_box_route() -> None:
    rules = SecretRules(allow_private_upstreams=True)
    await rules.put(
        vm="box-one",
        name="openai",
        host="127.0.0.1:8443",
        placeholder="placeholder",
        value="secret",
    )

    rules.delete(vm="box-one", name="openai")

    assert rules.names(vm="box-one") == []


@pytest.mark.asyncio
async def test_rules_reject_a_shared_placeholder_for_two_secrets() -> None:
    rules = SecretRules(allow_private_upstreams=True)
    values = {
        "vm": "box-one",
        "host": "127.0.0.1:8443",
        "placeholder": "same-placeholder",
    }
    await rules.put(name="first", value="first-secret", **values)

    with pytest.raises(SecretProxyRejectedError, match="conflict"):
        await rules.put(
            vm="box-one",
            name="second",
            host="127.0.0.1:8443",
            placeholder="same-placeholder",
            value="second-secret",
        )
