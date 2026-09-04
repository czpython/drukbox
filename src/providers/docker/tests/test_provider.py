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
async def test_create_vm_adds_service_authorized_keys():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    await provider.create_vm(
        name="sb-test",
        image="img",
        authorized_keys=("ssh-ed25519 AAAATUNNEL",),
    )

    keys = api.run_container.await_args.kwargs["env"]["DRUKBOX_AUTHORIZED_KEY"].splitlines()
    assert keys[0].startswith("ssh-ed25519 ")
    assert keys[1] == "ssh-ed25519 AAAATUNNEL"


@pytest.mark.asyncio
async def test_create_vm_passes_service_setup_script_to_the_entrypoint():
    api = _api_mock()
    provider = DockerProvider(api, _settings())

    await provider.create_vm(name="sb-test", image="img", env={}, setup_script="#!/bin/sh\n")

    assert api.run_container.await_args.kwargs["env"]["DRUKBOX_SETUP_SCRIPT"] == "#!/bin/sh\n"


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
