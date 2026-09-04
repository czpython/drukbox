from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.exceptions import ProviderNotFoundError, ProviderTransportError
from providers.hetzner.exceptions import HetznerTransportError
from providers.hetzner.provider import HetznerProvider
from providers.hetzner.settings import HetznerSettings


def _settings(**overrides: Any) -> HetznerSettings:
    base: dict[str, Any] = {
        "api_token": "hetzner-token",
        "location": "nbg1",
        "server_type": "cx23",
    }
    base.update(overrides)
    return HetznerSettings(**base)


def _api_mock() -> MagicMock:
    api = MagicMock()
    api.ensure_ssh_key = AsyncMock()
    api.delete_ssh_key = AsyncMock()
    api.create_server = AsyncMock(return_value="12345")
    api.wait_for_running_with_ip = AsyncMock(return_value="203.0.113.7")
    api.find_server_id_by_name = AsyncMock(return_value=None)
    api.delete_server = AsyncMock()
    return api


@pytest.mark.asyncio
async def test_create_vm_mints_key_and_returns_public_ip_and_private_key():
    api = _api_mock()
    provider = HetznerProvider(api, _settings())

    result = await provider.create_vm(
        name="sb-test",
        image="ubuntu-24.04",
        env={"FOO": "bar"},
        setup_script="echo hi",
        authorized_keys=("ssh-ed25519 AAAATUNNEL",),
    )

    api.ensure_ssh_key.assert_awaited_once()
    key_kwargs = api.ensure_ssh_key.await_args.kwargs
    assert key_kwargs["name"] == "drukbox-sb-test"
    assert key_kwargs["labels"] == {"managed-by": "drukbox", "drukbox-host-name": "sb-test"}

    server_kwargs = api.create_server.await_args.kwargs
    assert server_kwargs["name"] == "sb-test"
    assert server_kwargs["image"] == "ubuntu-24.04"
    assert server_kwargs["ssh_key_name"] == "drukbox-sb-test"
    # The bootstrap script gets the caller env prepended as shell exports.
    assert "export FOO=bar" in server_kwargs["user_data"]
    assert "ssh-ed25519 AAAATUNNEL" in server_kwargs["user_data"]

    assert result.ssh_host == "203.0.113.7"
    assert result.ssh_port == 22
    assert result.ssh_username == "root"
    assert result.private_key
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in result.private_key


@pytest.mark.asyncio
async def test_create_vm_passes_instance_type_as_server_type():
    api = _api_mock()
    provider = HetznerProvider(api, _settings())

    await provider.create_vm(
        name="sb-test",
        image="ubuntu-24.04",
        env={},
        setup_script="echo hi",
        instance_type="cx33",
    )

    assert api.create_server.await_args.kwargs["server_type"] == "cx33"


@pytest.mark.asyncio
async def test_create_vm_deletes_key_when_create_server_fails():
    api = _api_mock()
    api.create_server.side_effect = HetznerTransportError("boom")
    provider = HetznerProvider(api, _settings())

    with pytest.raises(ProviderTransportError):
        await provider.create_vm(
            name="sb-test", image="ubuntu-24.04", env={}, setup_script="echo hi"
        )
    api.delete_ssh_key.assert_awaited_once_with("drukbox-sb-test")


@pytest.mark.asyncio
async def test_create_vm_uses_custom_ssh_username():
    api = _api_mock()
    provider = HetznerProvider(api, _settings(ssh_username="sandbox"))

    result = await provider.create_vm(
        name="sb-test", image="ubuntu-24.04", env={}, setup_script="echo hi"
    )
    assert result.ssh_username == "sandbox"


@pytest.mark.asyncio
async def test_delete_vm_deletes_server_and_key():
    api = _api_mock()
    api.find_server_id_by_name.return_value = "12345"
    provider = HetznerProvider(api, _settings())

    await provider.delete_vm("sb-test")

    api.delete_server.assert_awaited_once_with("12345")
    api.delete_ssh_key.assert_awaited_once_with("drukbox-sb-test")


@pytest.mark.asyncio
async def test_delete_vm_raises_not_found_when_server_missing():
    api = _api_mock()
    api.find_server_id_by_name.return_value = None
    provider = HetznerProvider(api, _settings())

    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-missing")
    api.delete_server.assert_not_called()


@pytest.mark.asyncio
async def test_delete_vm_deletes_key_even_when_server_already_gone():
    # A prior teardown may have deleted the server but failed on the key; the
    # key must still be reclaimed, not stranded behind the not-found short-circuit.
    api = _api_mock()
    api.find_server_id_by_name.return_value = None
    provider = HetznerProvider(api, _settings())

    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")
    api.delete_ssh_key.assert_awaited_once_with("drukbox-sb-test")
    api.delete_server.assert_not_called()


def test_default_image_reads_from_settings():
    provider = HetznerProvider(_api_mock(), _settings(default_image="debian-12"))
    assert provider.default_image == "debian-12"
