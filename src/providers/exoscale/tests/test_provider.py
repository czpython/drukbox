from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.exceptions import ProviderNotFoundError, ProviderTransportError
from providers.exoscale.exceptions import ExoscaleTransportError
from providers.exoscale.provider import ExoscaleProvider
from providers.exoscale.settings import ExoscaleSettings


def _settings(**overrides: Any) -> ExoscaleSettings:
    base: dict[str, Any] = {
        "api_key": "exo-key",
        "api_secret": "exo-secret",
        "zone": "ch-gva-2",
    }
    base.update(overrides)
    return ExoscaleSettings(**base)


def _api_mock() -> MagicMock:
    api = MagicMock()
    api.ensure_ssh_key = AsyncMock()
    api.delete_ssh_key = AsyncMock()
    api.create_instance = AsyncMock(return_value="i-12345")
    api.wait_for_running_with_ip = AsyncMock(return_value="198.51.100.7")
    api.find_instance_id_by_name = AsyncMock(return_value=None)
    api.delete_instance = AsyncMock()
    return api


@pytest.mark.asyncio
async def test_create_vm_mints_key_and_returns_public_ip_and_private_key():
    api = _api_mock()
    provider = ExoscaleProvider(api, _settings())

    result = await provider.create_vm(
        name="sb-test",
        image="Linux Ubuntu 24.04 LTS 64-bit",
        env={"FOO": "bar"},
        authorized_keys=("ssh-ed25519 AAAATUNNEL",),
    )

    api.ensure_ssh_key.assert_awaited_once()
    key_kwargs = api.ensure_ssh_key.await_args.kwargs
    assert key_kwargs["name"] == "drukbox-sb-test"
    assert key_kwargs["labels"] == {"managed-by": "drukbox", "drukbox-host-name": "sb-test"}

    instance_kwargs = api.create_instance.await_args.kwargs
    assert instance_kwargs["name"] == "sb-test"
    assert instance_kwargs["image"] == "Linux Ubuntu 24.04 LTS 64-bit"
    assert instance_kwargs["ssh_key_name"] == "drukbox-sb-test"
    assert instance_kwargs["labels"] == {"managed-by": "drukbox", "drukbox-host-name": "sb-test"}
    assert instance_kwargs["user_data"].startswith("#!/bin/sh\n")
    assert "export FOO=bar" in instance_kwargs["user_data"]
    assert "ssh-ed25519 AAAATUNNEL" in instance_kwargs["user_data"]

    assert result.ssh_host == "198.51.100.7"
    assert result.ssh_port == 22
    assert result.ssh_username == "ubuntu"
    assert result.private_key
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in result.private_key


@pytest.mark.asyncio
async def test_create_vm_passes_instance_type():
    api = _api_mock()
    provider = ExoscaleProvider(api, _settings())

    await provider.create_vm(
        name="sb-test",
        image="Linux Ubuntu 24.04 LTS 64-bit",
        env={},
        setup_script="echo hi",
        instance_type="standard.large",
    )

    assert api.create_instance.await_args.kwargs["instance_type"] == "standard.large"


@pytest.mark.asyncio
async def test_create_vm_passes_disk_gb():
    api = _api_mock()
    provider = ExoscaleProvider(api, _settings())

    await provider.create_vm(
        name="sb-test",
        image="Linux Ubuntu 24.04 LTS 64-bit",
        env={},
        setup_script="echo hi",
        disk_gb=80,
    )

    assert api.create_instance.await_args.kwargs["disk_gb"] == 80


@pytest.mark.asyncio
async def test_create_vm_uses_default_disk_gb_from_settings():
    api = _api_mock()
    provider = ExoscaleProvider(api, _settings(disk_gb=60))

    await provider.create_vm(
        name="sb-test",
        image="Linux Ubuntu 24.04 LTS 64-bit",
        env={},
        setup_script="echo hi",
    )

    assert api.create_instance.await_args.kwargs["disk_gb"] == 60


@pytest.mark.asyncio
async def test_create_vm_deletes_key_when_create_instance_fails():
    api = _api_mock()
    api.create_instance.side_effect = ExoscaleTransportError("boom")
    provider = ExoscaleProvider(api, _settings())

    with pytest.raises(ProviderTransportError):
        await provider.create_vm(
            name="sb-test",
            image="Linux Ubuntu 24.04 LTS 64-bit",
            env={},
            setup_script="echo hi",
        )
    api.delete_ssh_key.assert_awaited_once_with("drukbox-sb-test")


@pytest.mark.asyncio
async def test_create_vm_uses_custom_ssh_username():
    api = _api_mock()
    provider = ExoscaleProvider(api, _settings(ssh_username="sandbox"))

    result = await provider.create_vm(
        name="sb-test",
        image="Linux Ubuntu 24.04 LTS 64-bit",
        env={},
        setup_script="echo hi",
    )
    assert result.ssh_username == "sandbox"


@pytest.mark.asyncio
async def test_delete_vm_deletes_instance_and_key():
    api = _api_mock()
    api.find_instance_id_by_name.return_value = "i-12345"
    provider = ExoscaleProvider(api, _settings())

    await provider.delete_vm("sb-test")

    api.delete_instance.assert_awaited_once_with("i-12345")
    api.delete_ssh_key.assert_awaited_once_with("drukbox-sb-test")


@pytest.mark.asyncio
async def test_delete_vm_raises_not_found_when_instance_missing():
    api = _api_mock()
    api.find_instance_id_by_name.return_value = None
    provider = ExoscaleProvider(api, _settings())

    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-missing")
    api.delete_instance.assert_not_called()


@pytest.mark.asyncio
async def test_delete_vm_deletes_key_even_when_instance_already_gone():
    api = _api_mock()
    api.find_instance_id_by_name.return_value = None
    provider = ExoscaleProvider(api, _settings())

    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")
    api.delete_ssh_key.assert_awaited_once_with("drukbox-sb-test")
    api.delete_instance.assert_not_called()


def test_default_image_reads_from_settings():
    provider = ExoscaleProvider(_api_mock(), _settings(default_image="Debian 12"))
    assert provider.default_image == "Debian 12"
