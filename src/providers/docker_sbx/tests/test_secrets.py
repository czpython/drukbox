import stat
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from host_secrets.catalog import CATALOG, Service, Upstream
from host_secrets.placeholder import Placeholder
from providers.docker_sbx.exceptions import DockerSbxTransportError
from providers.docker_sbx.secrets import SbxInjection
from providers.exceptions import ProviderCommandError, ProviderTransportError


def _api_mock() -> MagicMock:
    api = MagicMock()
    api.set_secret = AsyncMock()
    api.set_custom_secret = AsyncMock()
    api.remove_secret = AsyncMock()
    api.remove_custom_secret = AsyncMock()
    api.custom_placeholders = AsyncMock(return_value=[])
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
        hosts=["api.anthropic.com"],
        env="ANTHROPIC_AUTH_TOKEN",
        placeholder=str(placeholder),
        command=f"cat {path}",
    )
    api.set_secret.assert_not_awaited()
    assert environment == {"ANTHROPIC_AUTH_TOKEN": str(placeholder)}


async def test_a_pushed_value_is_a_rewritten_file(tmp_path: Path) -> None:
    api = _api_mock()
    injection = SbxInjection(api, tmp_path)
    placeholder = Placeholder.mint(uuid.uuid4(), "anthropic")
    await injection.put_secret(
        vm="sb-one", service=CATALOG["anthropic"], placeholder=placeholder, value="old"
    )

    await injection.push_secret(vm="sb-one", name="anthropic", value="new")

    path = tmp_path / "sb-one" / "anthropic"
    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path], "nothing staged is left behind"
    assert api.set_custom_secret.await_count == 1, "sbx reads the file at each use"


async def test_a_push_after_teardown_brings_nothing_back(tmp_path: Path) -> None:
    api = _api_mock()
    injection = SbxInjection(api, tmp_path)
    placeholder = Placeholder.mint(uuid.uuid4(), "anthropic")
    await injection.put_secret(
        vm="sb-one", service=CATALOG["anthropic"], placeholder=placeholder, value="old"
    )
    await injection.delete_secrets(vm="sb-one")

    with pytest.raises(ProviderCommandError, match="value file"):
        await injection.push_secret(vm="sb-one", name="anthropic", value="new")

    assert not (tmp_path / "sb-one").exists()


async def test_a_new_value_replaces_the_file_and_the_secret(tmp_path: Path) -> None:
    api = _api_mock()
    injection = SbxInjection(api, tmp_path)
    placeholder = Placeholder.mint(uuid.uuid4(), "anthropic")
    service = CATALOG["anthropic"]

    await injection.put_secret(vm="sb-one", service=service, placeholder=placeholder, value="old")
    await injection.put_secret(vm="sb-one", service=service, placeholder=placeholder, value="new")

    assert (tmp_path / "sb-one" / "anthropic").read_text() == "new"
    assert api.set_custom_secret.await_count == 2


async def test_a_custom_entry_named_github_is_a_custom_secret_on_its_host(tmp_path: Path) -> None:
    api = _api_mock()
    placeholder = Placeholder.mint(uuid.uuid4(), "github")
    enterprise = Service("GH_ENTERPRISE_TOKEN", (Upstream("github.enterprise.example"),))

    environment = await SbxInjection(api, tmp_path).put_secret(
        vm="sb-one", service=enterprise, placeholder=placeholder, value="ghe_real"
    )

    api.set_custom_secret.assert_awaited_once_with(
        sandbox="sb-one",
        hosts=["github.enterprise.example"],
        env="GH_ENTERPRISE_TOKEN",
        placeholder=str(placeholder),
        command=f"cat {tmp_path / 'sb-one' / 'github'}",
    )
    api.set_secret.assert_not_awaited()
    assert environment == {"GH_ENTERPRISE_TOKEN": str(placeholder)}


async def test_delete_secrets_removes_the_scope_and_the_files(tmp_path: Path) -> None:
    api = _api_mock()
    injection = SbxInjection(api, tmp_path)
    anthropic = Placeholder.mint(uuid.uuid4(), "anthropic")
    github = Placeholder.mint(uuid.uuid4(), "github")
    await injection.put_secret(
        vm="sb-one", service=CATALOG["anthropic"], placeholder=anthropic, value="sk-ant-real"
    )
    await injection.put_secret(
        vm="sb-one", service=CATALOG["github"], placeholder=github, value="ghs_real"
    )
    api.custom_placeholders.return_value = [str(anthropic), "drk.x.other.y"]

    await injection.delete_secrets(vm="sb-one")

    api.remove_secret.assert_awaited_once_with("github", sandbox="sb-one")
    api.custom_placeholders.assert_awaited_once_with(sandbox="sb-one")
    assert [call.kwargs for call in api.remove_custom_secret.await_args_list] == [
        {"sandbox": "sb-one", "placeholder": str(anthropic)},
        {"sandbox": "sb-one", "placeholder": "drk.x.other.y"},
    ]
    assert not (tmp_path / "sb-one").exists()


async def test_delete_secrets_can_run_again_after_a_partial_teardown(tmp_path: Path) -> None:
    api = _api_mock()

    await SbxInjection(api, tmp_path).delete_secrets(vm="sb-one")

    api.remove_secret.assert_awaited_once_with("github", sandbox="sb-one")
    api.remove_custom_secret.assert_not_awaited()
    assert not (tmp_path / "sb-one").exists()


@pytest.mark.parametrize("service", ["github", "anthropic"])
async def test_a_cli_failure_is_a_provider_error(tmp_path: Path, service: str) -> None:
    api = _api_mock()
    api.set_secret.side_effect = DockerSbxTransportError("daemon unavailable")
    api.set_custom_secret.side_effect = DockerSbxTransportError("daemon unavailable")
    api.remove_secret.side_effect = DockerSbxTransportError("daemon unavailable")
    injection = SbxInjection(api, tmp_path)
    placeholder = Placeholder.mint(uuid.uuid4(), service)

    with pytest.raises(ProviderTransportError, match="daemon unavailable"):
        await injection.put_secret(
            vm="sb-one", service=CATALOG[service], placeholder=placeholder, value="v"
        )
    with pytest.raises(ProviderTransportError, match="daemon unavailable"):
        await injection.delete_secrets(vm="sb-one")
    # The value file stays for the retry.
    assert (tmp_path / "sb-one" / service).exists()
