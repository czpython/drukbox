from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.aws.exceptions import AwsTransportError
from providers.aws.provider import AWSProvider
from providers.aws.settings import AwsSettings
from providers.exceptions import ProviderNotFoundError, ProviderTransportError


def _settings(**overrides: Any) -> AwsSettings:
    base: dict[str, Any] = {
        "region": "eu-central-1",
        "default_image": "ami-deadbeef",
        "instance_type": "t3.medium",
    }
    base.update(overrides)
    return AwsSettings(**base)


def _api_mock() -> MagicMock:
    api = MagicMock()
    api.resolve_ssm_parameter = AsyncMock(return_value="ami-deadbeef")
    api.import_key_pair = AsyncMock()
    api.delete_key_pair = AsyncMock()
    api.run_instance = AsyncMock(return_value="i-1234567890abcdef0")
    api.wait_for_running_with_ip = AsyncMock(return_value="203.0.113.5")
    api.find_instance_id_by_tag_name = AsyncMock(return_value=None)
    api.terminate_instance = AsyncMock()
    api.ensure_managed_security_group = AsyncMock(return_value="sg-managed")
    return api


@pytest.mark.asyncio
async def test_create_vm_with_tailscale_on_skips_keypair_sg_and_public_ip():
    api = _api_mock()
    provider = AWSProvider(api, _settings(), tailscale_enabled=True)

    result = await provider.create_vm(
        name="sb-test",
        image="ami-deadbeef",
        env={"TAILSCALE_AUTHKEY": "tskey-x"},
        setup_script="#!/usr/bin/env bash\necho hi\n",
        authorized_keys=("ssh-ed25519 AAAATUNNEL",),
    )

    api.import_key_pair.assert_not_called()
    api.ensure_managed_security_group.assert_not_called()
    api.wait_for_running_with_ip.assert_not_called()
    kwargs = api.run_instance.await_args.kwargs
    assert not kwargs["key_name"]
    assert kwargs["associate_public_ip"] is False
    assert not kwargs["security_group_id"]
    assert kwargs["tags"]["managed-by"] == "drukbox"
    assert kwargs["client_token"] == "sb-test"
    assert kwargs["instance_type"] == "t3.medium"
    assert kwargs["root_gb"] == 100
    assert "ssh-ed25519 AAAATUNNEL" in kwargs["user_data"]
    assert not result.private_key
    assert result.ssh_host == ""


@pytest.mark.asyncio
async def test_create_vm_with_tailscale_off_provisions_keypair_and_security_group(monkeypatch):
    api = _api_mock()
    provider = AWSProvider(api, _settings(), tailscale_enabled=False)

    async def _detect() -> str | None:
        return "198.51.100.7"

    monkeypatch.setattr("providers.aws.provider._detect_outbound_ipv4", _detect)

    result = await provider.create_vm(
        name="sb-test", image="ami-deadbeef", env={}, setup_script="echo hi"
    )

    api.import_key_pair.assert_awaited_once()
    api.ensure_managed_security_group.assert_awaited_once()
    api.wait_for_running_with_ip.assert_awaited_once_with("i-1234567890abcdef0")
    kwargs = api.run_instance.await_args.kwargs
    assert kwargs["key_name"] == "drukbox-sb-test"
    assert kwargs["associate_public_ip"] is True
    assert kwargs["security_group_id"] == "sg-managed"
    sg_kwargs = api.ensure_managed_security_group.await_args.kwargs
    assert sg_kwargs["ingress_cidrs"] == ("198.51.100.7/32",)
    # The IP literal, never the public DNS name — EC2 public DNS resolves to
    # the private IP inside the VPC, which the /32 above never matches.
    assert result.ssh_host == "203.0.113.5"
    assert result.private_key
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in result.private_key


@pytest.mark.asyncio
async def test_managed_sg_is_created_in_the_subnets_vpc(monkeypatch):
    # With a custom subnet, the managed SG must land in that subnet's VPC or EC2
    # rejects the launch. Resolve the subnet's VPC and pass it through.
    api = _api_mock()
    api.resolve_subnet_vpc = AsyncMock(return_value="vpc-abc")
    provider = AWSProvider(
        api, _settings(subnet_id="subnet-xyz", ssh_cidrs="10.0.0.0/8"), tailscale_enabled=False
    )

    await provider.create_vm(name="sb-test", image="ami-deadbeef", env={}, setup_script="echo hi")

    api.resolve_subnet_vpc.assert_awaited_once_with("subnet-xyz")
    assert api.ensure_managed_security_group.await_args.kwargs["vpc_id"] == "vpc-abc"


@pytest.mark.asyncio
async def test_create_vm_falls_back_to_world_open_when_detection_fails(monkeypatch):
    api = _api_mock()
    provider = AWSProvider(api, _settings(), tailscale_enabled=False)

    async def _no_detect() -> str | None:
        return None

    monkeypatch.setattr("providers.aws.provider._detect_outbound_ipv4", _no_detect)

    await provider.create_vm(name="sb-test", image="ami-deadbeef", env={}, setup_script="echo hi")

    sg_kwargs = api.ensure_managed_security_group.await_args.kwargs
    assert sg_kwargs["ingress_cidrs"] == ("0.0.0.0/0",)


@pytest.mark.asyncio
async def test_world_open_fallback_is_not_cached_and_self_heals(monkeypatch):
    # A transient detection failure must not pin the shared SG to 0.0.0.0/0 for
    # the provider's lifetime: the next create re-detects and narrows it.
    api = _api_mock()
    provider = AWSProvider(api, _settings(), tailscale_enabled=False)
    detected: list[str | None] = [None, "198.51.100.7"]

    async def _detect() -> str | None:
        return detected.pop(0)

    monkeypatch.setattr("providers.aws.provider._detect_outbound_ipv4", _detect)

    await provider.create_vm(name="sb-1", image="ami-deadbeef", env={}, setup_script="echo hi")
    await provider.create_vm(name="sb-2", image="ami-deadbeef", env={}, setup_script="echo hi")

    assert api.ensure_managed_security_group.await_count == 2
    assert api.ensure_managed_security_group.await_args.kwargs["ingress_cidrs"] == (
        "198.51.100.7/32",
    )


@pytest.mark.asyncio
async def test_create_vm_resolves_ssm_path_to_ami_id():
    api = _api_mock()
    api.resolve_ssm_parameter.return_value = "ami-resolved"
    provider = AWSProvider(api, _settings(), tailscale_enabled=True)

    await provider.create_vm(
        name="sb-test",
        image="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/ami-id",
        env={},
        setup_script="echo hi",
    )

    api.resolve_ssm_parameter.assert_awaited_once_with(
        "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/ami-id"
    )
    assert api.run_instance.await_args.kwargs["ami_id"] == "ami-resolved"


@pytest.mark.asyncio
async def test_create_vm_cleans_up_keypair_when_run_instance_fails(monkeypatch):
    api = _api_mock()
    api.run_instance.side_effect = AwsTransportError("boom")
    provider = AWSProvider(api, _settings(), tailscale_enabled=False)

    async def _no_detect() -> str | None:
        return None

    monkeypatch.setattr("providers.aws.provider._detect_outbound_ipv4", _no_detect)

    with pytest.raises(ProviderTransportError):
        await provider.create_vm(
            name="sb-test", image="ami-deadbeef", env={}, setup_script="echo hi"
        )
    api.delete_key_pair.assert_awaited_once_with("drukbox-sb-test")


@pytest.mark.asyncio
async def test_delete_vm_terminates_instance_and_deletes_keypair():
    api = _api_mock()
    api.find_instance_id_by_tag_name.return_value = "i-real"
    provider = AWSProvider(api, _settings(), tailscale_enabled=True)

    await provider.delete_vm("sb-test")

    api.find_instance_id_by_tag_name.assert_awaited_once_with("sb-test", managed_by="drukbox")
    api.terminate_instance.assert_awaited_once_with("i-real")
    api.delete_key_pair.assert_awaited_once_with("drukbox-sb-test")


@pytest.mark.asyncio
async def test_delete_vm_scopes_lookup_to_custom_service_label():
    # The managed-by lookup filter must match the label create_vm tags with, or a
    # custom SERVICE_LABEL deployment can't find (and silently orphans) its VMs.
    api = _api_mock()
    api.find_instance_id_by_tag_name.return_value = "i-real"
    provider = AWSProvider(api, _settings(), tailscale_enabled=True, service_label="staging")

    await provider.delete_vm("sb-test")

    api.find_instance_id_by_tag_name.assert_awaited_once_with("sb-test", managed_by="staging")
    api.terminate_instance.assert_awaited_once_with("i-real")


@pytest.mark.asyncio
async def test_delete_vm_raises_provider_not_found_when_instance_missing():
    api = _api_mock()
    api.find_instance_id_by_tag_name.return_value = None
    provider = AWSProvider(api, _settings(), tailscale_enabled=True)

    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-missing")
    api.terminate_instance.assert_not_called()
    api.delete_key_pair.assert_not_called()


@pytest.mark.asyncio
async def test_create_vm_with_ssh_cidrs_set_does_not_auto_detect(monkeypatch):
    api = _api_mock()
    provider = AWSProvider(
        api,
        _settings(ssh_cidrs="10.0.0.0/8, 192.168.1.0/24"),
        tailscale_enabled=False,
    )

    detect_calls = 0

    async def _spy_detect() -> str | None:
        nonlocal detect_calls
        detect_calls += 1
        return "203.0.113.5"

    monkeypatch.setattr("providers.aws.provider._detect_outbound_ipv4", _spy_detect)

    await provider.create_vm(name="sb-test", image="ami-deadbeef", env={}, setup_script="echo hi")

    assert detect_calls == 0
    sg_kwargs = api.ensure_managed_security_group.await_args.kwargs
    assert sg_kwargs["ingress_cidrs"] == ("10.0.0.0/8", "192.168.1.0/24")


@pytest.mark.asyncio
async def test_create_vm_passes_custom_root_gb_to_run_instance():
    api = _api_mock()
    provider = AWSProvider(api, _settings(root_gb=250), tailscale_enabled=True)

    await provider.create_vm(name="sb-test", image="ami-deadbeef", env={}, setup_script="echo hi")
    assert api.run_instance.await_args.kwargs["root_gb"] == 250


@pytest.mark.asyncio
async def test_create_vm_honors_per_request_sizing_over_settings():
    api = _api_mock()
    provider = AWSProvider(api, _settings(), tailscale_enabled=True)

    await provider.create_vm(
        name="sb-test",
        image="ami-deadbeef",
        env={},
        setup_script="echo hi",
        instance_type="t3.xlarge",
        disk_gb=250,
    )

    kwargs = api.run_instance.await_args.kwargs
    assert kwargs["instance_type"] == "t3.xlarge"
    assert kwargs["root_gb"] == 250


@pytest.mark.asyncio
async def test_create_vm_populates_ssh_username_from_settings():
    api = _api_mock()
    provider = AWSProvider(api, _settings(ssh_username="ec2-user"), tailscale_enabled=True)

    result = await provider.create_vm(
        name="sb-test", image="ami-deadbeef", env={}, setup_script="echo hi"
    )
    assert result.ssh_username == "ec2-user"


def test_default_image_reads_from_aws_settings():
    api = _api_mock()
    provider = AWSProvider(
        api,
        _settings(default_image="ami-fallback"),
        tailscale_enabled=True,
    )
    assert provider.default_image == "ami-fallback"
