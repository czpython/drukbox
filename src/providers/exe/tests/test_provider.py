from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from core.settings import get_settings
from providers.docker.api import DockerAPI
from providers.docker.exceptions import DockerImageNotFoundError, DockerTransportError
from providers.exceptions import ProviderCommandError, ProviderNotFoundError, ProviderTransportError
from providers.exe.api import ExeAPI
from providers.exe.exceptions import ExeIntegrationAlreadyExistsError, ExeVMNotFoundError
from providers.exe.provider import ExeProvider
from providers.exe.settings import ExeSettings


def _settings(**overrides: Any) -> ExeSettings:
    defaults: dict[str, Any] = {"api_token": "test-token", "default_image": "img:latest"}
    return ExeSettings(**{**defaults, **overrides})


def _docker_mock() -> SimpleNamespace:
    return SimpleNamespace(
        build_image=AsyncMock(),
        remove_image=AsyncMock(),
        push_image=AsyncMock(return_value="ghcr.io/acme/templates@sha256:" + "a" * 64),
        aclose=AsyncMock(),
    )


def _make_provider(api: object) -> ExeProvider:
    return ExeProvider(api, _settings(), docker=_docker_mock())  # type: ignore[arg-type]


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


@pytest.fixture
def registry_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "registry_host", "ghcr.io")
    monkeypatch.setattr(settings, "template_repository", "")
    monkeypatch.setattr(settings, "registry_username", "bot")
    monkeypatch.setattr(settings, "registry_password", SecretStr("secret"))


@pytest.mark.parametrize("suffix", [":abc123", "@sha256:" + "a" * 64, ":build@sha256:" + "a" * 64])
async def test_create_vm_sends_registry_auth_without_template_configuration(
    registry_settings, suffix
):
    api = SimpleNamespace(create_vm=AsyncMock(return_value=_vm_payload()))
    provider = _make_provider(api)

    await provider.create_vm(name="sb-1", image=f"ghcr.io/acme/private-base{suffix}")

    assert api.create_vm.await_args.kwargs["registry_auth"] == "bot:secret"


@pytest.mark.parametrize(
    "image",
    [
        "docker.io/library/ubuntu:24.04",
        "ghcr.io.evil.example/acme/templates:tag",
        "other.example/acme/templates:tag",
        "ghcr.io:5000/acme/templates:tag",
    ],
)
async def test_create_vm_keeps_credentials_off_other_registry_hosts(registry_settings, image):
    api = SimpleNamespace(create_vm=AsyncMock(return_value=_vm_payload()))
    provider = _make_provider(api)

    await provider.create_vm(name="sb-1", image=image)

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


async def test_aclose_closes_both_clients() -> None:
    api = SimpleNamespace(aclose=AsyncMock())
    docker = _docker_mock()
    provider = ExeProvider(api, _settings(), docker=docker)  # type: ignore[arg-type]

    await provider.aclose()
    api.aclose.assert_awaited_once_with()
    docker.aclose.assert_awaited_once_with()


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


_ACME = {
    "name": "acme",
    "host": "api.acme.test",
    "credential_header": "Authorization",
    "credential_prefix": "Bearer ",
    "credential_var": "ACME_TOKEN",
    "endpoint_var": "ACME_BASE_URL",
}


async def test_put_secret_creates_and_attaches_box_scoped_integration() -> None:
    api = SimpleNamespace(
        create_http_proxy=AsyncMock(),
        attach_http_proxy=AsyncMock(),
    )
    provider = _make_provider(api)

    env = await provider.put_secret(vm="sb-one", service=_ACME, value="secret-value")

    api.create_http_proxy.assert_awaited_once_with(
        name="sb-one--acme",
        target="https://api.acme.test",
        headers={"Authorization": "Bearer secret-value"},
    )
    api.attach_http_proxy.assert_awaited_once_with("sb-one--acme", attach_vm="sb-one")
    assert env == {"ACME_BASE_URL": "https://sb-one--acme.int.exe.xyz"}


async def test_put_secret_updates_existing_box_scoped_integration() -> None:
    api = SimpleNamespace(
        create_http_proxy=AsyncMock(side_effect=ExeIntegrationAlreadyExistsError("already exists")),
        update_http_proxy=AsyncMock(),
        attach_http_proxy=AsyncMock(),
    )
    provider = _make_provider(api)

    await provider.put_secret(vm="sb-one", service=_ACME, value="new-value")

    api.update_http_proxy.assert_awaited_once_with(
        name="sb-one--acme",
        target="https://api.acme.test",
        headers={"Authorization": "Bearer new-value"},
    )
    api.attach_http_proxy.assert_awaited_once_with("sb-one--acme", attach_vm="sb-one")


async def test_put_secret_uses_the_service_auth_shape() -> None:
    """A service that authenticates with its own header gets that header, not a bearer."""
    api = SimpleNamespace(create_http_proxy=AsyncMock(), attach_http_proxy=AsyncMock())
    provider = _make_provider(api)

    await provider.put_secret(
        vm="sb-one",
        service={**_ACME, "credential_header": "x-api-key", "credential_prefix": ""},
        value="secret-value",
    )

    assert api.create_http_proxy.await_args.kwargs["headers"] == {"x-api-key": "secret-value"}


async def test_delete_secret_deletes_only_box_scoped_integration() -> None:
    api = SimpleNamespace(delete_http_proxy=AsyncMock())
    provider = _make_provider(api)

    await provider.delete_secret(vm="sb-one", name="acme")

    api.delete_http_proxy.assert_awaited_once_with("sb-one--acme")


async def test_list_secrets_returns_names_for_only_requested_box() -> None:
    api = SimpleNamespace(
        list_http_proxies=AsyncMock(
            return_value=[
                "sb-two--github",
                "sb-one--github",
                "account-proxy",
                "sb-one--acme",
            ]
        )
    )
    provider = _make_provider(api)

    names = await provider.list_secrets(vm="sb-one")

    assert names == ["acme", "github"]


def test_from_settings_constructs_with_exeapi() -> None:
    provider = ExeProvider.from_settings()
    assert isinstance(provider.api, ExeAPI)
    assert isinstance(provider.docker, DockerAPI)


async def test_build_template_image_builds_and_returns_digest(
    registry_settings, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "template_repository", "acme/templates")
    docker = _docker_mock()
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        docker=docker,  # type: ignore[arg-type]
    )

    image = await provider.build_template_image(
        base_image="exe/base:latest",
        setup_script="apt-get update",
        label="Node tools",
    )

    assert image == "ghcr.io/acme/templates@sha256:" + "a" * 64
    tag = docker.build_image.await_args.args[0]
    assert tag.startswith("ghcr.io/acme/templates:node-tools-")
    docker.push_image.assert_awaited_once_with(tag, username="bot", password="secret")


async def test_build_template_image_names_each_missing_registry_setting() -> None:
    docker = _docker_mock()
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        docker=docker,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderCommandError) as error:
        await provider.build_template_image(
            base_image="exe/base:latest",
            setup_script="apt-get update",
            label="Node tools",
        )

    assert "TEMPLATE_REPOSITORY" in str(error.value)
    assert "REGISTRY_USERNAME" in str(error.value)
    assert "REGISTRY_PASSWORD" in str(error.value)
    docker.build_image.assert_not_awaited()


async def test_build_template_image_translates_push_failure(registry_settings, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "template_repository", "acme/templates")
    docker = _docker_mock()
    docker.push_image.side_effect = DockerTransportError("push log tail")
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        docker=docker,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderTransportError, match="push log tail"):
        await provider.build_template_image(
            base_image="exe/base:latest",
            setup_script="apt-get update",
            label="Node tools",
        )


async def test_delete_template_image_removes_the_local_image() -> None:
    docker = _docker_mock()
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        docker=docker,  # type: ignore[arg-type]
    )

    await provider.delete_template_image("ghcr.io/acme/drukbox-templates:123456789abc")

    docker.remove_image.assert_awaited_once_with("ghcr.io/acme/drukbox-templates:123456789abc")


async def test_delete_template_image_translates_a_missing_local_image() -> None:
    docker = _docker_mock()
    docker.remove_image.side_effect = DockerImageNotFoundError("No such image")
    provider = ExeProvider(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        docker=docker,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderNotFoundError, match="was not found"):
        await provider.delete_template_image("ghcr.io/acme/drukbox-templates:missing")
