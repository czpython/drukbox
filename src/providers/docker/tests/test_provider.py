from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.docker.exceptions import (
    DockerImageNotFoundError,
    DockerTransportError,
    DockerVMNotFoundError,
)
from providers.docker.provider import DockerProvider
from providers.docker.settings import DockerSettings
from providers.exceptions import (
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)


def _settings(**overrides: Any) -> DockerSettings:
    return DockerSettings(**overrides)


def _api_mock() -> MagicMock:
    api = MagicMock()
    api.run_container = AsyncMock(return_value="container-id")
    api.published_ssh_port = AsyncMock(return_value=49160)
    api.remove_container = AsyncMock()
    api.build_image = AsyncMock()
    api.remove_image = AsyncMock()
    api.server_version = AsyncMock(return_value="27.0.3")
    api.run_shell = AsyncMock(return_value="")
    return api


@pytest.mark.asyncio
async def test_create_vm_runs_container_and_returns_loopback_coords():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    result = await provider.create_vm(name="sb-test", image="drukbox/sandbox:latest", env={})

    run_kwargs = api.run_container.await_args.kwargs
    assert run_kwargs["name"] == "sb-test"
    assert run_kwargs["image"] == "drukbox/sandbox:latest"
    assert run_kwargs["labels"] == {"managed-by": "drukbox", "drukbox-host-name": "sb-test"}
    # The public key is injected so the container's entrypoint can seed authorized_keys.
    assert run_kwargs["env"]["DRUKBOX_AUTHORIZED_KEY"].startswith("ssh-ed25519 ")

    assert result.ssh_host == "127.0.0.1"
    assert result.ssh_port == 49160
    assert result.ssh_username == "root"
    assert result.private_key
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in result.private_key


@pytest.mark.asyncio
async def test_create_vm_passes_caller_env_and_names_it_for_the_entrypoint():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    await provider.create_vm(name="sb-test", image="img", env={"FOO": "bar"})

    container_env = api.run_container.await_args.kwargs["env"]
    assert container_env["FOO"] == "bar"
    assert container_env["DRUKBOX_ENV_KEYS"] == "FOO"


@pytest.mark.asyncio
async def test_create_vm_rejects_setup_script_because_tailscale_is_unsupported():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    with pytest.raises(ProviderCommandError):
        await provider.create_vm(name="sb-test", image="img", env={}, setup_script="#!/bin/sh\n")
    api.run_container.assert_not_called()


@pytest.mark.asyncio
async def test_create_vm_rejects_caller_env_that_collides_with_reserved_keys():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    with pytest.raises(ProviderCommandError, match="reserved"):
        await provider.create_vm(
            name="sb-test",
            image="img",
            env={"DRUKBOX_AUTHORIZED_KEY": "ssh-ed25519 attacker"},
        )
    api.run_container.assert_not_called()


@pytest.mark.asyncio
async def test_create_vm_removes_container_when_port_lookup_fails():
    api = _api_mock()
    api.published_ssh_port.side_effect = DockerTransportError("no port")
    provider = DockerProvider(api, _settings())

    with pytest.raises(ProviderTransportError):
        await provider.create_vm(name="sb-test", image="img", env={})
    api.remove_container.assert_awaited_once_with("sb-test")


@pytest.mark.asyncio
async def test_create_vm_port_lookup_error_survives_failed_cleanup():
    # If removing the half-started container also fails, the caller must still
    # see the original ProviderTransportError, not the Docker-specific cleanup
    # exception leaking past the adapter boundary.
    api = _api_mock()
    api.published_ssh_port.side_effect = DockerTransportError("no port")
    api.remove_container.side_effect = DockerTransportError("cleanup failed")
    provider = DockerProvider(api, _settings())

    with pytest.raises(ProviderTransportError, match="no port"):
        await provider.create_vm(name="sb-test", image="img", env={})
    api.remove_container.assert_awaited_once_with("sb-test")


@pytest.mark.asyncio
async def test_delete_vm_removes_container():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    await provider.delete_vm("sb-test")

    api.remove_container.assert_awaited_once_with("sb-test")


@pytest.mark.asyncio
async def test_delete_vm_raises_not_found_when_container_missing():
    api = _api_mock()
    api.remove_container.side_effect = DockerVMNotFoundError("No such container: sb-test")
    provider = DockerProvider(api, _settings())

    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")


@pytest.mark.asyncio
async def test_build_template_image_builds_and_returns_the_derived_tag():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    image = await provider.build_template_image(
        base_image="sandbox:base",
        setup_script="apt-get update",
        label="Node tools",
    )

    assert image.startswith("drukbox-template:")
    assert len(image.removeprefix("drukbox-template:")) == 12
    assert api.build_image.await_args.args[0] == image


@pytest.mark.asyncio
async def test_build_template_image_translates_build_failure():
    api = _api_mock()
    api.build_image.side_effect = DockerTransportError("build log tail")
    provider = DockerProvider(api, _settings())

    with pytest.raises(ProviderTransportError, match="build log tail"):
        await provider.build_template_image(
            base_image="sandbox:base",
            setup_script="apt-get update",
            label="Node tools",
        )


@pytest.mark.asyncio
async def test_delete_template_image_removes_the_local_image():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    await provider.delete_template_image("drukbox-template:123456789abc")

    api.remove_image.assert_awaited_once_with("drukbox-template:123456789abc")


@pytest.mark.asyncio
async def test_delete_template_image_translates_a_missing_image():
    api = _api_mock()
    api.remove_image.side_effect = DockerImageNotFoundError("No such image")
    provider = DockerProvider(api, _settings())

    with pytest.raises(ProviderNotFoundError, match="was not found"):
        await provider.delete_template_image("drukbox-template:missing")


@pytest.mark.asyncio
async def test_diagnose_returns_server_version():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    assert await provider.diagnose() == "docker server 27.0.3"


def test_default_image_reads_from_settings():
    provider = DockerProvider(_api_mock(), _settings(default_image="my/sandbox:v2"))
    assert provider.default_image == "my/sandbox:v2"


_GITHUB = {
    "name": "github",
    "host": "api.github.com",
    "credential_header": "Authorization",
    "credential_prefix": "Bearer ",
    "credential_var": "GH_TOKEN",
    "endpoint_var": "",
}
_ANTHROPIC = {
    **_GITHUB,
    "name": "anthropic",
    "host": "api.anthropic.com",
    "credential_var": "ANTHROPIC_AUTH_TOKEN",
    "endpoint_var": "ANTHROPIC_BASE_URL",
}


@pytest.mark.asyncio
async def test_put_secret_writes_the_placeholder_and_the_edge_address_into_the_box():
    api = _api_mock()
    provider = DockerProvider(api, _settings(), secrets_exchange_url="http://172.17.0.1:8080")

    env = await provider.put_secret(vm="sb-one", service=_ANTHROPIC, value="drk.abc.anthropic.x")

    assert env == {
        "ANTHROPIC_AUTH_TOKEN": "drk.abc.anthropic.x",
        "ANTHROPIC_BASE_URL": "http://172.17.0.1:8080/api.anthropic.com",
    }
    name, script = api.run_shell.await_args.args
    assert name == "sb-one"
    assert "sed -i '/^ANTHROPIC_AUTH_TOKEN=/d' /etc/environment" in script
    assert "ANTHROPIC_AUTH_TOKEN=drk.abc.anthropic.x" in script
    assert "ANTHROPIC_BASE_URL=http://172.17.0.1:8080/api.anthropic.com" in script


@pytest.mark.asyncio
async def test_put_secret_writes_no_endpoint_for_a_service_without_a_base_url_variable():
    api = _api_mock()
    provider = DockerProvider(api, _settings(), secrets_exchange_url="http://172.17.0.1:8080")

    env = await provider.put_secret(vm="sb-one", service=_GITHUB, value="drk.abc.github.x")

    assert env == {"GH_TOKEN": "drk.abc.github.x"}


@pytest.mark.asyncio
async def test_put_secret_needs_the_edge_address():
    provider = DockerProvider(_api_mock(), _settings())

    with pytest.raises(ProviderCommandError, match="SECRETS_EXCHANGE_URL"):
        await provider.put_secret(vm="sb-one", service=_GITHUB, value="drk.abc.github.x")


@pytest.mark.asyncio
async def test_list_and_delete_secrets_read_the_placeholders_back():
    api = _api_mock()
    host_hex = "0" * 32
    api.run_shell.return_value = (
        f"GH_TOKEN=drk.{host_hex}.github.abc\nANTHROPIC_AUTH_TOKEN=drk.{host_hex}.anthropic.def\n"
        "ANTHROPIC_BASE_URL=http://172.17.0.1:8080/api.anthropic.com\nOTHER=value\n"
    )
    provider = DockerProvider(api, _settings(), secrets_exchange_url="http://172.17.0.1:8080")

    assert await provider.list_secrets(vm="sb-one") == ["anthropic", "github"]

    await provider.delete_secret(vm="sb-one", name="github")
    _, script = api.run_shell.await_args.args
    assert script.startswith("sed -i -E") and ".github\\./d" in script


@pytest.mark.asyncio
async def test_put_secret_on_a_missing_container_is_a_not_found_error():
    api = _api_mock()
    api.run_shell = AsyncMock(side_effect=DockerVMNotFoundError("gone"))
    provider = DockerProvider(api, _settings(), secrets_exchange_url="http://172.17.0.1:8080")

    with pytest.raises(ProviderNotFoundError):
        await provider.put_secret(vm="sb-one", service=_GITHUB, value="drk.abc.github.x")
