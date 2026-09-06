import stat
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from host_secrets.catalog import CATALOG
from host_secrets.placeholder import Placeholder
from providers.docker_sbx.exceptions import DockerSbxTransportError
from providers.docker_sbx.secrets import SbxInjection
from providers.exceptions import ProviderTransportError


def _api_mock() -> MagicMock:
    api = MagicMock()
    api.set_secret = AsyncMock()
    api.set_custom_secret = AsyncMock()
    api.remove_secret = AsyncMock()
    api.remove_custom_secret = AsyncMock()
    return api


def test_sbx_holds_the_value() -> None:
    assert SbxInjection.holds_value is True


async def test_a_github_secret_goes_to_sbx_own_github_service_from_a_file(tmp_path: Path) -> None:
    api = _api_mock()
    placeholder = Placeholder.mint(uuid.uuid4(), "github")

    environment = await SbxInjection(api, tmp_path).put_secret(
        vm="sb-one", service=CATALOG["github"], placeholder=placeholder, value="ghs_real"
    )

    path = tmp_path / "sb-one" / "github"
    assert path.read_text() == "ghs_real"
    api.set_secret.assert_awaited_once_with("github", sandbox="sb-one", command=f"cat {path}")
    api.set_custom_secret.assert_not_awaited()
    assert environment == {"GH_TOKEN": str(placeholder)}


async def test_any_other_secret_is_a_custom_secret_on_its_host_from_a_file(tmp_path: Path) -> None:
    api = _api_mock()
    placeholder = Placeholder.mint(uuid.uuid4(), "anthropic")

    environment = await SbxInjection(api, tmp_path).put_secret(
        vm="sb-one", service=CATALOG["anthropic"], placeholder=placeholder, value="sk-ant-real"
    )

    path = tmp_path / "sb-one" / "anthropic"
    assert path.read_text() == "sk-ant-real"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    api.set_custom_secret.assert_awaited_once_with(
        sandbox="sb-one",
        host="api.anthropic.com",
        env="ANTHROPIC_AUTH_TOKEN",
        placeholder=str(placeholder),
        command=f"cat {path}",
    )
    api.set_secret.assert_not_awaited()
    assert environment == {"ANTHROPIC_AUTH_TOKEN": str(placeholder)}


async def test_a_new_value_replaces_the_file_and_the_secret(tmp_path: Path) -> None:
    api = _api_mock()
    injection = SbxInjection(api, tmp_path)
    placeholder = Placeholder.mint(uuid.uuid4(), "anthropic")
    service = CATALOG["anthropic"]

    await injection.put_secret(vm="sb-one", service=service, placeholder=placeholder, value="old")
    await injection.put_secret(vm="sb-one", service=service, placeholder=placeholder, value="new")

    assert (tmp_path / "sb-one" / "anthropic").read_text() == "new"
    assert api.set_custom_secret.await_count == 2


async def test_delete_removes_a_custom_secret_by_placeholder_and_its_file(tmp_path: Path) -> None:
    api = _api_mock()
    injection = SbxInjection(api, tmp_path)
    placeholder = Placeholder.mint(uuid.uuid4(), "anthropic")
    await injection.put_secret(
        vm="sb-one", service=CATALOG["anthropic"], placeholder=placeholder, value="sk-ant-real"
    )

    await injection.delete_secret(vm="sb-one", placeholder=placeholder)

    api.remove_custom_secret.assert_awaited_once_with(
        sandbox="sb-one", placeholder=str(placeholder)
    )
    assert not (tmp_path / "sb-one" / "anthropic").exists()


async def test_delete_removes_a_github_secret_by_service_name_and_its_file(tmp_path: Path) -> None:
    api = _api_mock()
    injection = SbxInjection(api, tmp_path)
    placeholder = Placeholder.mint(uuid.uuid4(), "github")
    await injection.put_secret(
        vm="sb-one", service=CATALOG["github"], placeholder=placeholder, value="ghs_real"
    )

    await injection.delete_secret(vm="sb-one", placeholder=placeholder)

    api.remove_secret.assert_awaited_once_with("github", sandbox="sb-one")
    api.remove_custom_secret.assert_not_awaited()
    assert not (tmp_path / "sb-one" / "github").exists()


@pytest.mark.parametrize("service", ["github", "anthropic"])
async def test_a_cli_failure_is_a_provider_error(tmp_path: Path, service: str) -> None:
    api = _api_mock()
    api.set_secret.side_effect = DockerSbxTransportError("daemon unavailable")
    api.set_custom_secret.side_effect = DockerSbxTransportError("daemon unavailable")
    api.remove_secret.side_effect = DockerSbxTransportError("daemon unavailable")
    api.remove_custom_secret.side_effect = DockerSbxTransportError("daemon unavailable")
    injection = SbxInjection(api, tmp_path)
    placeholder = Placeholder.mint(uuid.uuid4(), service)

    with pytest.raises(ProviderTransportError, match="daemon unavailable"):
        await injection.put_secret(
            vm="sb-one", service=CATALOG[service], placeholder=placeholder, value="v"
        )
    with pytest.raises(ProviderTransportError, match="daemon unavailable"):
        await injection.delete_secret(vm="sb-one", placeholder=placeholder)
