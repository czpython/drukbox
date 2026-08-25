from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from providers.docker.api import DockerCLI
from providers.docker.exceptions import DockerImageNotFoundError, DockerTransportError
from providers.exceptions import ProviderCommandError, ProviderNotFoundError, ProviderTransportError
from providers.exe.api import ExeAPI
from providers.exe.exceptions import ExeVMNotFoundError
from providers.exe.provider import ExeProvider
from providers.exe.settings import ExeSettings


def _settings(**overrides: Any) -> ExeSettings:
    defaults: dict[str, Any] = {"api_token": "test-token", "default_image": "img:latest"}
    return ExeSettings(**{**defaults, **overrides})


def _docker_mock() -> SimpleNamespace:
    return SimpleNamespace(
        build_image=AsyncMock(),
        remove_image=AsyncMock(),
        push_image=AsyncMock(),
        login=AsyncMock(),
    )


def _make_provider(api: object) -> ExeProvider:
    return ExeProvider(api, _settings(), docker_cli=_docker_mock())  # type: ignore[arg-type]


async def test_create_vm_forwards_kwargs_and_maps_result() -> None:
    api = SimpleNamespace(
        create_vm=AsyncMock(
            return_value={
                "vm_name": "sb-1234",
                "ssh_port": "2222",
                "ssh_dest": "sb-1234.public.exe.dev",
            }
        ),
    )
    provider = _make_provider(api)

    result = await provider.create_vm(
        name="sb-1234",
        image="img:latest",
        env={"K": "V"},
        setup_script="#!/bin/bash\necho hello",
    )

    # ExeProvider constructs its own tags from the name + service_label.
    api.create_vm.assert_awaited_once_with(
        name="sb-1234",
        image="img:latest",
        env={"K": "V"},
        setup_script="#!/bin/bash\necho hello",
        tags=["managed-by-drukbox"],
        registry_auth=None,
    )
    assert result.provider_id == "sb-1234"
    assert result.name == "sb-1234"
    assert result.ssh_port == 2222
    assert result.ssh_host == "sb-1234.public.exe.dev"


def _vm_payload() -> dict[str, str]:
    return {"vm_name": "sb-1", "ssh_port": "22", "ssh_dest": "sb-1.public.exe.dev"}


async def test_create_vm_sends_registry_auth_for_template_registry_images() -> None:
    api = SimpleNamespace(create_vm=AsyncMock(return_value=_vm_payload()))
    settings = _settings(
        template_registry="ghcr.io/acme/templates",
        registry_username="bot",
        registry_password="secret",
    )
    provider = ExeProvider(api, settings, docker_cli=_docker_mock())  # type: ignore[arg-type]

    await provider.create_vm(name="sb-1", image="ghcr.io/acme/templates:abc123")

    assert api.create_vm.await_args.kwargs["registry_auth"] == "bot:secret"


async def test_create_vm_keeps_credentials_off_other_registries() -> None:
    api = SimpleNamespace(create_vm=AsyncMock(return_value=_vm_payload()))
    settings = _settings(
        template_registry="ghcr.io/acme/templates",
        registry_username="bot",
        registry_password="secret",
    )
    provider = ExeProvider(api, settings, docker_cli=_docker_mock())  # type: ignore[arg-type]

    await provider.create_vm(name="sb-1", image="docker.io/library/ubuntu:24.04")

    assert api.create_vm.await_args.kwargs["registry_auth"] is None


async def test_create_vm_omits_registry_auth_when_registry_is_not_configured() -> None:
    api = SimpleNamespace(create_vm=AsyncMock(return_value=_vm_payload()))
    provider = _make_provider(api)

    await provider.create_vm(name="sb-1", image="img:latest")

    assert api.create_vm.await_args.kwargs["registry_auth"] is None


async def test_delete_vm_delegates_to_api() -> None:
    api = SimpleNamespace(delete_vm=AsyncMock())
    provider = _make_provider(api)

    await provider.delete_vm("sb-1234")
    api.delete_vm.assert_awaited_once_with("sb-1234")


async def test_delete_vm_translates_not_found_to_provider_not_found() -> None:
    api = SimpleNamespace(
        delete_vm=AsyncMock(side_effect=ExeVMNotFoundError("vm 'sb-1' not found")),
    )
    provider = _make_provider(api)

    with pytest.raises(ProviderNotFoundError, match="sb-1"):
        await provider.delete_vm("sb-1")


async def test_aclose_delegates_to_api() -> None:
    api = SimpleNamespace(aclose=AsyncMock())
    provider = _make_provider(api)

    await provider.aclose()
    api.aclose.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        (
            "create_http_proxy",
            {"name": "p", "target": "https://t", "headers": {"H": "v"}},
        ),
        ("delete_http_proxy", {"name": "p"}),
        ("attach_http_proxy", {"name": "p", "attach_vm": "sb-1234"}),
        ("detach_http_proxy", {"name": "p", "attach_vm": "sb-1234"}),
    ],
)
async def test_http_proxy_methods_delegate_to_api(method_name: str, kwargs: dict) -> None:
    api = SimpleNamespace(**{method_name: AsyncMock()})
    provider = _make_provider(api)

    proxy_kwargs = dict(kwargs)
    positional: list[str] = []

    if method_name in {"delete_http_proxy", "attach_http_proxy", "detach_http_proxy"}:
        positional.append(proxy_kwargs.pop("name"))
    await getattr(provider, method_name)(*positional, **proxy_kwargs)
    getattr(api, method_name).assert_awaited_once_with(*positional, **proxy_kwargs)


def test_from_settings_constructs_with_exeapi() -> None:
    provider = ExeProvider.from_settings()
    assert isinstance(provider.api, ExeAPI)
    assert isinstance(provider.docker_cli, DockerCLI)


async def test_create_template_builds_logs_in_and_pushes() -> None:
    docker = _docker_mock()
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(
            template_registry="ghcr.io/acme/drukbox-templates",
            registry_username="builder",
            registry_password="registry-secret",
        ),
        docker_cli=docker,  # type: ignore[arg-type]
    )

    handle = await provider.create_template(
        base_image="exe/base:latest",
        setup_script="apt-get update",
        label="Node tools",
    )

    assert handle.startswith("ghcr.io/acme/drukbox-templates:")
    assert len(handle.rpartition(":")[2]) == 12
    assert docker.build_image.await_args.args[0] == handle
    docker.login.assert_awaited_once_with("ghcr.io", "builder", "registry-secret")
    docker.push_image.assert_awaited_once_with(handle)


async def test_create_template_names_each_missing_registry_setting() -> None:
    docker = _docker_mock()
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        docker_cli=docker,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderCommandError) as error:
        await provider.create_template(
            base_image="exe/base:latest",
            setup_script="apt-get update",
            label="Node tools",
        )

    assert "EXE_TEMPLATE_REGISTRY" in str(error.value)
    assert "EXE_REGISTRY_USERNAME" in str(error.value)
    assert "EXE_REGISTRY_PASSWORD" in str(error.value)
    docker.build_image.assert_not_awaited()


async def test_create_template_translates_push_failure() -> None:
    docker = _docker_mock()
    docker.push_image.side_effect = DockerTransportError("push log tail")
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(
            template_registry="ghcr.io/acme/drukbox-templates",
            registry_username="builder",
            registry_password="registry-secret",
        ),
        docker_cli=docker,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderTransportError, match="push log tail"):
        await provider.create_template(
            base_image="exe/base:latest",
            setup_script="apt-get update",
            label="Node tools",
        )


async def test_delete_template_removes_the_local_image() -> None:
    docker = _docker_mock()
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        docker_cli=docker,  # type: ignore[arg-type]
    )

    await provider.delete_template("ghcr.io/acme/drukbox-templates:123456789abc")

    docker.remove_image.assert_awaited_once_with("ghcr.io/acme/drukbox-templates:123456789abc")


async def test_delete_template_translates_a_missing_local_image() -> None:
    docker = _docker_mock()
    docker.remove_image.side_effect = DockerImageNotFoundError("No such image")
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        docker_cli=docker,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderNotFoundError, match="was not found"):
        await provider.delete_template("ghcr.io/acme/drukbox-templates:missing")
