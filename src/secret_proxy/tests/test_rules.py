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
            host="127.0.0.1:8443",
            env_var="API_TOKEN",
            headers={"Authorization": "Bearer first-placeholder"},
            placeholder="first-placeholder",
            value="first-secret",
        ),
        rules.put(
            vm="box-one",
            host="127.0.0.1:8443",
            env_var="API_TOKEN",
            headers={"Authorization": "Bearer second-placeholder"},
            placeholder="second-placeholder",
            value="second-secret",
        ),
    )

    assert rules.names(vm="box-one") == ["API_TOKEN"]
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
            host="127.0.0.1",
            env_var="API_TOKEN",
            headers={},
            placeholder="placeholder",
            value="secret",
        )


@pytest.mark.asyncio
async def test_route_authentication_is_box_scoped() -> None:
    rules = SecretRules(allow_private_upstreams=True)
    await rules.put(
        vm="box-one",
        host="127.0.0.1:8443",
        env_var="API_TOKEN",
        headers={},
        placeholder="placeholder",
        value="secret",
    )

    route = rules.route(vm="box-one")

    assert rules.authenticate(vm="box-one", token=route["password"])
    assert not rules.authenticate(vm="box-two", token=route["password"])
    assert not rules.for_host(vm="box-one", host="127.0.0.1:9443")


@pytest.mark.asyncio
async def test_delete_removes_the_last_box_route() -> None:
    rules = SecretRules(allow_private_upstreams=True)
    await rules.put(
        vm="box-one",
        host="127.0.0.1:8443",
        env_var="API_TOKEN",
        headers={},
        placeholder="placeholder",
        value="secret",
    )

    rules.delete(vm="box-one", env_var="API_TOKEN")

    assert rules.names(vm="box-one") == []
    with pytest.raises(SecretProxyRejectedError, match="no registered secret route"):
        rules.route(vm="box-one")


@pytest.mark.asyncio
async def test_rules_reject_a_shared_placeholder_for_two_secrets() -> None:
    rules = SecretRules(allow_private_upstreams=True)
    values = {
        "vm": "box-one",
        "host": "127.0.0.1:8443",
        "headers": {"X-First": "same-placeholder"},
        "placeholder": "same-placeholder",
    }
    await rules.put(env_var="FIRST_TOKEN", value="first-secret", **values)

    with pytest.raises(SecretProxyRejectedError, match="conflict"):
        await rules.put(
            vm="box-one",
            host="127.0.0.1:8443",
            env_var="SECOND_TOKEN",
            headers={"X-Second": "same-placeholder"},
            placeholder="same-placeholder",
            value="second-secret",
        )


@pytest.mark.asyncio
async def test_rules_reject_two_secret_templates_for_one_header() -> None:
    rules = SecretRules(allow_private_upstreams=True)
    await rules.put(
        vm="box-one",
        host="127.0.0.1:8443",
        env_var="FIRST_TOKEN",
        headers={"Authorization": "Bearer first-placeholder"},
        placeholder="first-placeholder",
        value="first-secret",
    )

    with pytest.raises(SecretProxyRejectedError, match="conflict"):
        await rules.put(
            vm="box-one",
            host="127.0.0.1:8443",
            env_var="SECOND_TOKEN",
            headers={"authorization": "Bearer second-placeholder"},
            placeholder="second-placeholder",
            value="second-secret",
        )


@pytest.mark.asyncio
async def test_rules_reject_registered_routing_headers() -> None:
    rules = SecretRules(allow_private_upstreams=True)

    with pytest.raises(SecretProxyRejectedError, match="headers are invalid"):
        await rules.put(
            vm="box-one",
            host="127.0.0.1:8443",
            env_var="API_TOKEN",
            headers={"Host": "other.example.com"},
            placeholder="placeholder",
            value="secret",
        )
