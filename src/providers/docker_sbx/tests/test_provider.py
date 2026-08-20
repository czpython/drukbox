from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.docker_sbx.exceptions import (
    DockerSbxNotFoundError,
    DockerSbxTransportError,
)
from providers.docker_sbx.provider import DockerSbxProvider
from providers.docker_sbx.settings import DockerSbxSettings
from providers.exceptions import (
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)


def _settings(workspace_root: Path, **overrides: Any) -> DockerSbxSettings:
    return DockerSbxSettings(workspace_root=workspace_root, **overrides)


def _api_mock() -> MagicMock:
    api = MagicMock()
    api.create_sandbox = AsyncMock()
    api.run_bootstrap = AsyncMock()
    api.publish_ssh_port = AsyncMock(return_value=49160)
    api.remove_sandbox = AsyncMock()
    api.sandbox_count = AsyncMock(return_value=2)
    return api


@pytest.mark.asyncio
async def test_create_vm_creates_a_sized_sandbox_and_returns_published_coords(tmp_path):
    api = _api_mock()
    provider = DockerSbxProvider(api, _settings(tmp_path, cpus=4, memory="8g"))

    result = await provider.create_vm(name="sb-test", image="drukbox/sbx-sandbox:latest", env={})

    create_kwargs = api.create_sandbox.await_args.kwargs
    assert create_kwargs["name"] == "sb-test"
    assert create_kwargs["template"] == "drukbox/sbx-sandbox:latest"
    assert create_kwargs["cpus"] == 4
    assert create_kwargs["memory"] == "8g"
    # The daemon reads the workspace path on its own filesystem. The
    # directory must exist before the sandbox creation.
    assert create_kwargs["workspace"] == str(tmp_path / "sb-test")
    assert (tmp_path / "sb-test").is_dir()

    api.publish_ssh_port.assert_awaited_once_with("sb-test")
    assert result.ssh_host == "127.0.0.1"
    assert result.ssh_port == 49160
    assert result.ssh_username == "root"
    assert result.private_key is not None
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in result.private_key


@pytest.mark.asyncio
async def test_create_vm_bootstrap_installs_the_public_key_and_caller_env(tmp_path):
    api = _api_mock()
    provider = DockerSbxProvider(api, _settings(tmp_path))

    await provider.create_vm(name="sb-test", image="img", env={"API_TOKEN": "s3cr3t"})

    name, script = api.run_bootstrap.await_args.args
    assert name == "sb-test"
    assert "ssh-ed25519 " in script
    assert "/root/.ssh/authorized_keys" in script
    # The caller environment goes to SSH sessions through pam_env.
    assert "API_TOKEN=s3cr3t" in script
    assert "/etc/environment" in script


@pytest.mark.asyncio
async def test_create_vm_installs_the_key_for_the_configured_ssh_user(tmp_path):
    api = _api_mock()
    provider = DockerSbxProvider(api, _settings(tmp_path, ssh_username="dev"))

    result = await provider.create_vm(name="sb-test", image="img", env={})

    _, script = api.run_bootstrap.await_args.args
    # The key must go where sshd examines it for the given user. If not,
    # the host becomes ACTIVE but refuses each login.
    assert "/home/dev/.ssh/authorized_keys" in script
    assert result.ssh_username == "dev"


@pytest.mark.asyncio
async def test_create_vm_rejects_setup_script_because_tailscale_is_unsupported(tmp_path):
    api = _api_mock()
    provider = DockerSbxProvider(api, _settings(tmp_path))

    # The service does not send a script, because supports_tailnet is
    # False. This guard finds a defect in the caller.
    with pytest.raises(ProviderCommandError, match="Tailscale"):
        await provider.create_vm(name="sb-test", image="img", env={}, setup_script="#!/bin/sh\n")
    api.create_sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_create_vm_rejects_env_values_that_would_forge_environment_entries(tmp_path):
    api = _api_mock()
    provider = DockerSbxProvider(api, _settings(tmp_path))

    with pytest.raises(ProviderCommandError, match="NUL or newline"):
        await provider.create_vm(name="sb-test", image="img", env={"EVIL": "value\nINJECTED=x"})
    api.create_sandbox.assert_not_called()
    assert not (tmp_path / "sb-test").exists()


@pytest.mark.asyncio
async def test_create_vm_cleans_up_when_the_sandbox_cannot_be_created(tmp_path):
    api = _api_mock()
    api.create_sandbox.side_effect = DockerSbxTransportError("daemon unavailable")
    provider = DockerSbxProvider(api, _settings(tmp_path))

    with pytest.raises(ProviderTransportError):
        await provider.create_vm(name="sb-test", image="img", env={})
    # The CLI can stop after the daemon makes the sandbox. A failed create
    # also tries the removal.
    api.remove_sandbox.assert_awaited_once_with("sb-test")
    assert not (tmp_path / "sb-test").exists()


@pytest.mark.asyncio
async def test_create_vm_translates_an_unwritable_workspace_root(tmp_path):
    api = _api_mock()
    # A file at the workspace root location makes mkdir fail. The error is
    # the same OSError type as for a missing bind mount.
    blocked_root = tmp_path / "blocked"
    blocked_root.touch()
    provider = DockerSbxProvider(api, _settings(blocked_root))

    with pytest.raises(ProviderTransportError, match="workspace"):
        await provider.create_vm(name="sb-test", image="img", env={})
    api.create_sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_create_vm_tears_down_sandbox_and_workspace_when_publishing_fails(tmp_path):
    api = _api_mock()
    api.publish_ssh_port.side_effect = DockerSbxTransportError("no port")
    provider = DockerSbxProvider(api, _settings(tmp_path))

    with pytest.raises(ProviderTransportError):
        await provider.create_vm(name="sb-test", image="img", env={})
    api.remove_sandbox.assert_awaited_once_with("sb-test")
    assert not (tmp_path / "sb-test").exists()


@pytest.mark.asyncio
async def test_create_vm_publish_error_survives_failed_cleanup(tmp_path):
    # The cleanup of the sandbox can also fail. The caller must get the
    # first error, not the cleanup error.
    api = _api_mock()
    api.publish_ssh_port.side_effect = DockerSbxTransportError("no port")
    api.remove_sandbox.side_effect = DockerSbxTransportError("cleanup failed")
    provider = DockerSbxProvider(api, _settings(tmp_path))

    with pytest.raises(ProviderTransportError, match="no port"):
        await provider.create_vm(name="sb-test", image="img", env={})
    api.remove_sandbox.assert_awaited_once_with("sb-test")


@pytest.mark.asyncio
async def test_delete_vm_removes_the_sandbox_and_its_workspace(tmp_path):
    api = _api_mock()
    workspace = tmp_path / "sb-test"
    workspace.mkdir(parents=True)
    provider = DockerSbxProvider(api, _settings(tmp_path))

    await provider.delete_vm("sb-test")

    api.remove_sandbox.assert_awaited_once_with("sb-test")
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_delete_vm_drops_the_workspace_of_a_sandbox_that_never_existed(tmp_path):
    api = _api_mock()
    api.remove_sandbox.side_effect = DockerSbxNotFoundError("sandbox 'sb-test' not found")
    workspace = tmp_path / "sb-test"
    workspace.mkdir(parents=True)
    provider = DockerSbxProvider(api, _settings(tmp_path))

    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_delete_vm_keeps_the_workspace_when_teardown_fails(tmp_path):
    # The sandbox may still be running on the workspace, and HostService keeps
    # the row so deletion can be retried.
    api = _api_mock()
    api.remove_sandbox.side_effect = DockerSbxTransportError("daemon unavailable")
    workspace = tmp_path / "sb-test"
    workspace.mkdir(parents=True)
    provider = DockerSbxProvider(api, _settings(tmp_path))

    with pytest.raises(ProviderTransportError):
        await provider.delete_vm("sb-test")
    assert workspace.is_dir()
