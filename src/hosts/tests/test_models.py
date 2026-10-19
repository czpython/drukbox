import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from uuid6 import uuid7

from core.database import async_session_factory
from hosts.exceptions import HostKeyScanError, ProvisioningFailedError
from hosts.models import Host, HostStatus
from hosts.service import HostService, utc_now
from networking.tailscale import (
    DeviceDiscoveryTimeoutError,
    JoinCredentials,
)
from providers.base import VMCreateResult
from providers.exceptions import ProviderAuthError
from providers.exe.settings import ExeSettings


async def _patch_provision_happy_path(monkeypatch, *, ssh_port: int = 22) -> dict[str, AsyncMock]:
    """Wire up the four external dependencies of provision() with mocks that
    take the host all the way to ACTIVE. Returns the mocks for assertion."""
    mocks = {
        "issue_join_credentials": AsyncMock(
            return_value=JoinCredentials(env={"TAILSCALE_AUTHKEY": "tskey-secret"}),
        ),
        "create_vm": AsyncMock(
            return_value=VMCreateResult(
                provider_id="exe-runtime-1",
                name="exe-runtime-1",
                ssh_port=ssh_port,
                ssh_username="exedev",
            ),
        ),
        "wait_for_device": AsyncMock(return_value="n123CNTRL"),
        "scan_known_hosts": AsyncMock(
            return_value=b"exe-runtime-1.example.ts.net ssh-ed25519 AAAATEST\n"
        ),
    }
    monkeypatch.setattr(
        "networking.tailscale.Tailscale.issue_join_credentials",
        mocks["issue_join_credentials"],
    )
    monkeypatch.setattr(
        "providers.exe.provider.ExeProvider.create_vm",
        mocks["create_vm"],
    )
    monkeypatch.setattr(
        "networking.tailscale.Tailscale.wait_for_device",
        mocks["wait_for_device"],
    )
    monkeypatch.setattr(
        "hosts.service.HostService.scan_known_hosts",
        mocks["scan_known_hosts"],
    )
    return mocks


def test_build_name_is_distinct_for_uuids_sharing_a_timestamp_prefix():
    # Regression: UUIDv7's first 48 bits are the millisecond timestamp,
    # so two concurrent creates produce host_ids whose leading hex is
    # identical. build_name() must derive from the random tail, not the
    # timestamp prefix, or it collides on the unique name index.
    a = uuid.UUID("019e8307-79af-7000-8000-000000000001")
    b = uuid.UUID("019e8307-79af-7000-8000-000000000002")
    # Sanity: the offending prefix really is identical.
    assert a.hex[:12] == b.hex[:12]
    # And the names build_name produces must not be.
    assert Host.build_name(a) != Host.build_name(b)


async def test_provision_walks_host_to_active(monkeypatch):
    host_id = uuid.UUID("00000000-0000-0000-0000-000000000101")
    host = await create_host_record(
        id=host_id,
        name="lb-sandbox-test",
        status="provisioning",
        image="ghcr.io/drukbox/custom-sandbox:provision-test",
        env={
            "SANDBOX_GATEWAY_URL": "wss://gateway.example.ts.net/daemon",
            "SANDBOX_GATEWAY_DAEMON_TOKEN": "gateway-daemon-token",
            "TAILSCALE_ADVERTISE_TAGS": "tag:sandbox",
        },
    )
    mocks = await _patch_provision_happy_path(monkeypatch)

    async with async_session_factory() as session:
        session.add(host)
        await HostService(session).provision(host)

    async with async_session_factory() as session:
        refreshed = await session.get(Host, host.id)
        assert refreshed is not None
        assert refreshed.name == "exe-runtime-1"
        assert refreshed.internal_ssh_host == "exe-runtime-1.example.ts.net"
        assert refreshed.external_ssh_host == ""
        assert refreshed.status == "active"
        assert refreshed.tailscale_device_id == "n123CNTRL"
        assert refreshed.known_hosts == "exe-runtime-1.example.ts.net ssh-ed25519 AAAATEST\n"
        assert refreshed.activated_at is not None

    mocks["issue_join_credentials"].assert_awaited_once_with(host_name="lb-sandbox-test")
    assert mocks["create_vm"].await_args is not None
    create_vm_kwargs = mocks["create_vm"].await_args.kwargs
    assert create_vm_kwargs["name"] == "lb-sandbox-test"
    assert create_vm_kwargs["image"] == "ghcr.io/drukbox/custom-sandbox:provision-test"
    # No per-request sizing on the row → the provider falls back to its
    # configured default size.
    assert create_vm_kwargs["instance_type"] is None
    assert create_vm_kwargs["disk_gb"] is None
    assert create_vm_kwargs["env"]["TAILSCALE_AUTHKEY"] == "tskey-secret"
    assert create_vm_kwargs["env"]["TAILSCALE_ADVERTISE_TAGS"] == "tag:sandbox"
    assert create_vm_kwargs["env"]["SANDBOX_GATEWAY_URL"] == "wss://gateway.example.ts.net/daemon"
    # No drukbox-side callbacks injected — the box never phones home.
    assert "REMOTE_HOST_ANNOUNCE_URL" not in create_vm_kwargs["env"]
    assert "REMOTE_HOST_ANNOUNCE_TOKEN" not in create_vm_kwargs["env"]
    # Bootstrap script ships to the VM only knows about Tailscale.
    assert "TAILSCALE_AUTHKEY" in create_vm_kwargs["setup_script"]
    assert "REMOTE_HOST_ANNOUNCE" not in create_vm_kwargs["setup_script"]
    # wait_for_device is called against the post-create VM name (mirrors how
    # ExeProvider can rename hosts during creation).
    mocks["wait_for_device"].assert_awaited_once()
    assert mocks["wait_for_device"].await_args is not None
    wait_kwargs = mocks["wait_for_device"].await_args.kwargs
    assert wait_kwargs["host_name"] == "exe-runtime-1"


async def test_provision_forwards_sizing_from_the_row_to_create_vm(monkeypatch):
    # provision() reads sizing off the host row, not from request state, so a
    # janitor-retried or resumed provision still launches the requested size.
    host = await create_host_record(
        name="lb-sized",
        status="provisioning",
        instance_type="t3.xlarge",
        disk_gb=250,
    )
    mocks = await _patch_provision_happy_path(monkeypatch)

    async with async_session_factory() as session:
        session.add(host)
        await HostService(session).provision(host)

    assert mocks["create_vm"].await_args is not None
    create_vm_kwargs = mocks["create_vm"].await_args.kwargs
    assert create_vm_kwargs["instance_type"] == "t3.xlarge"
    assert create_vm_kwargs["disk_gb"] == 250


async def test_provision_threads_ssh_username_from_vm_result_onto_host(monkeypatch):
    # The provider knows which in-VM user the image runs as; provision()
    # must propagate it so the HostOut response (and subsequent GETs)
    # carry the right value without callers having to maintain their own
    # per-provider mapping.
    mocks = await _patch_provision_happy_path(monkeypatch)
    mocks["create_vm"].return_value = VMCreateResult(
        provider_id="exe-runtime-1",
        name="exe-runtime-1",
        ssh_port=22,
        ssh_username="exedev",
    )

    async with async_session_factory() as session:
        host = await HostService(session).create_host(env={}, image=None, expires_at=None)

    assert host.ssh_username == "exedev"


async def test_provision_threads_private_key_from_vm_result_onto_host(monkeypatch):
    # Providers that mint a per-VM keypair (AWS) return private key material
    # in VMCreateResult; provision() must propagate it onto the host instance
    # so the create-time HostOut response carries it.
    mocks = await _patch_provision_happy_path(monkeypatch)
    mocks["create_vm"].return_value = VMCreateResult(
        provider_id="exe-runtime-1",
        name="exe-runtime-1",
        ssh_port=22,
        ssh_username="exedev",
        private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END...\n",
    )

    async with async_session_factory() as session:
        host = await HostService(session).create_host(env={}, image=None, expires_at=None)

    assert host.private_key is not None
    assert host.private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")


async def test_create_host_clears_safety_ttl_when_caller_omits_one(monkeypatch):
    # The row is born with a safety TTL so the janitor reaps it if the
    # client disconnects mid-provision. On success, that safety TTL is
    # replaced with the caller's intent — here, NULL.
    await _patch_provision_happy_path(monkeypatch)

    async with async_session_factory() as session:
        created = await HostService(session).create_host(env={}, image=None, expires_at=None)

    assert created.expires_at is None


async def test_create_host_lands_on_caller_ttl_on_success(monkeypatch):
    await _patch_provision_happy_path(monkeypatch)
    caller_ttl = utc_now() + timedelta(hours=2)

    async with async_session_factory() as session:
        created = await HostService(session).create_host(env={}, image=None, expires_at=caller_ttl)

    assert created.expires_at is not None
    assert abs((created.expires_at - caller_ttl).total_seconds()) < 1


async def test_create_host_expires_errored_host_for_janitor(monkeypatch):
    # Strand protection on the unhappy path: if provisioning errors out,
    # expire the row immediately so the janitor reaps it (and its VM) on the
    # next cycle, without operators having to chase ERROR hosts manually.
    mocks = await _patch_provision_happy_path(monkeypatch)
    mocks["wait_for_device"].side_effect = DeviceDiscoveryTimeoutError(
        "Tailscale never surfaced device"
    )

    async with async_session_factory() as session:
        service = HostService(session)
        try:
            await service.create_host(env={}, image=None, expires_at=None)
        except ProvisioningFailedError:
            pass
        else:
            raise AssertionError("expected ProvisioningFailedError")

    # Row should still exist, expired so the janitor reaps it next cycle.
    async with async_session_factory() as session:
        result = await session.execute(select(Host).where(Host.status == "error"))
        errored = result.scalar_one()
        assert errored.expires_at is not None
        assert errored.expires_at <= utc_now()


async def test_provision_generated_authkey_overrides_caller_env(monkeypatch):
    # The service-level safety net: even if a caller somehow seeds
    # TAILSCALE_AUTHKEY (bypassing the schema's reserved-key validation),
    # the freshly issued key wins.
    host = await create_host_record(
        name="lb-sandbox-test",
        status="provisioning",
        env={"TAILSCALE_AUTHKEY": "caller-attempt"},
    )
    mocks = await _patch_provision_happy_path(monkeypatch)

    async with async_session_factory() as session:
        session.add(host)
        await HostService(session).provision(host)

    assert mocks["create_vm"].await_args is not None
    assert mocks["create_vm"].await_args.kwargs["env"]["TAILSCALE_AUTHKEY"] == "tskey-secret"


async def test_provision_marks_error_when_provider_rejects_credentials(monkeypatch):
    host = await create_host_record(name="lb-sandbox-test", status="provisioning")
    mocks = await _patch_provision_happy_path(monkeypatch)
    mocks["create_vm"].side_effect = ProviderAuthError("exe.dev authentication failed")

    async with async_session_factory() as session:
        session.add(host)
        await HostService(session).provision(host)

    async with async_session_factory() as session:
        refreshed = await session.get(Host, host.id)
        assert refreshed is not None
        assert refreshed.status == "error"
        assert refreshed.last_error == "ProviderAuthError: exe.dev authentication failed"


async def test_provision_marks_error_when_device_discovery_times_out(monkeypatch):
    host = await create_host_record(name="lb-sandbox-test", status="provisioning")
    mocks = await _patch_provision_happy_path(monkeypatch)
    mocks["wait_for_device"].side_effect = DeviceDiscoveryTimeoutError(
        "Tailscale did not surface device"
    )

    async with async_session_factory() as session:
        session.add(host)
        await HostService(session).provision(host)

    async with async_session_factory() as session:
        refreshed = await session.get(Host, host.id)
        assert refreshed is not None
        assert refreshed.status == "error"
        assert "Tailscale did not surface device" in refreshed.last_error
        # ssh-keyscan must not have been attempted if Tailscale never saw the device.
        mocks["scan_known_hosts"].assert_not_awaited()


async def test_provision_marks_error_when_keyscan_fails(monkeypatch):
    host = await create_host_record(name="lb-sandbox-test", status="provisioning")
    mocks = await _patch_provision_happy_path(monkeypatch)
    mocks["scan_known_hosts"].side_effect = HostKeyScanError("keyscan failed")

    async with async_session_factory() as session:
        session.add(host)
        await HostService(session).provision(host)

    async with async_session_factory() as session:
        refreshed = await session.get(Host, host.id)
        assert refreshed is not None
        assert refreshed.status == "error"
        assert "keyscan failed" in refreshed.last_error
        # Device id was already stored when Tailscale surfaced it; teardown
        # uses it to release the device even though we never reached ACTIVE.
        assert refreshed.tailscale_device_id == "n123CNTRL"


async def test_scan_known_hosts_retries_until_banner_is_returned(monkeypatch):
    # Mirrors the production race: tailscaled-SSH is not ready immediately
    # when the device shows up in Tailscale's API. First keyscan attempt
    # gets nothing back; a subsequent attempt succeeds.
    host = Host(
        name="lb-sandbox-test",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        internal_ssh_host="lb-sandbox-test.example.ts.net",
        external_ssh_host="",
        known_hosts="",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    empty_attempt = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"", b"")),
    )
    successful_attempt = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(b"lb-sandbox-test.example.ts.net ssh-ed25519 AAAATEST\n", b"")
        ),
    )
    create_subprocess_exec = AsyncMock(side_effect=[empty_attempt, successful_attempt])
    monkeypatch.setattr(
        "hosts.service.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )
    monkeypatch.setattr("hosts.service.asyncio.sleep", AsyncMock())

    async with async_session_factory() as session:
        service = HostService(session)
        known_hosts = await service.scan_known_hosts(host)

    assert known_hosts == b"lb-sandbox-test.example.ts.net ssh-ed25519 AAAATEST\n"
    assert create_subprocess_exec.await_count == 2


async def test_scan_known_hosts_fails_when_ssh_keyscan_cannot_run(monkeypatch):
    host = Host(
        name="sb-test",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        internal_ssh_host="sb-test.example.ts.net",
        external_ssh_host="",
        known_hosts="",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    monkeypatch.setattr(
        "hosts.service.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("ssh-keyscan")),
    )

    async with async_session_factory() as session:
        service = HostService(session)
        with pytest.raises(HostKeyScanError, match="could not run ssh-keyscan"):
            await service.scan_known_hosts(host)


async def test_scan_known_hosts_scans_both_internal_and_external(monkeypatch):
    # Tailscaled-SSH and the provider's edge sshd present different host keys;
    # callers picking either path need both entries to verify the connection.
    host = Host(
        name="sb-test",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        internal_ssh_host="sb-test.example.ts.net",
        external_ssh_host="sb-test.exe.xyz",
        external_ssh_port=22,
        known_hosts="",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                b"sb-test.example.ts.net ssh-ed25519 AAATAIL\nsb-test.exe.xyz ssh-rsa AAAEXE\n",
                b"",
            )
        ),
    )
    create_subprocess_exec = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "hosts.service.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )

    async with async_session_factory() as session:
        service = HostService(session)
        known_hosts = await service.scan_known_hosts(host)

    assert b"sb-test.example.ts.net" in known_hosts
    assert b"sb-test.exe.xyz" in known_hosts
    # Each address is scanned on its own port, so they get separate invocations.
    assert create_subprocess_exec.await_count == 2
    scanned_hosts = {call.args[-1] for call in create_subprocess_exec.await_args_list}
    assert scanned_hosts == {"sb-test.example.ts.net", "sb-test.exe.xyz"}


async def test_scan_known_hosts_retries_when_only_one_target_surfaces(monkeypatch):
    # First attempt: only the tailnet name comes back (tailscaled-SSH ready,
    # external edge sshd still warming up). Loop must retry until BOTH appear.
    host = Host(
        name="sb-test",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        internal_ssh_host="sb-test.example.ts.net",
        external_ssh_host="sb-test.exe.xyz",
        external_ssh_port=22,
        known_hosts="",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    internal_key = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"sb-test.example.ts.net ssh-ed25519 AAATAIL\n", b"")),
    )
    external_warming = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"", b"")),
    )
    external_key = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"sb-test.exe.xyz ssh-rsa AAAEXE\n", b"")),
    )
    # Attempt 1: internal answers, external sshd still warming. Attempt 2: both.
    create_subprocess_exec = AsyncMock(
        side_effect=[internal_key, external_warming, internal_key, external_key]
    )
    monkeypatch.setattr(
        "hosts.service.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )
    monkeypatch.setattr("hosts.service.asyncio.sleep", AsyncMock())

    async with async_session_factory() as session:
        service = HostService(session)
        known_hosts = await service.scan_known_hosts(host)

    assert b"sb-test.exe.xyz" in known_hosts
    assert create_subprocess_exec.await_count == 4


async def test_scan_known_hosts_uses_async_subprocess(monkeypatch):
    host = Host(
        name="lb-sandbox-test",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        internal_ssh_host="lb-sandbox-test.example.ts.net",
        external_ssh_host="",
        known_hosts="lb-sandbox-test.example.ts.net ssh-ed25519 AAAATEST\n",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(b"lb-sandbox-test.example.ts.net ssh-ed25519 AAAATEST\n", b"")
        ),
    )
    create_subprocess_exec = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "hosts.service.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )

    async with async_session_factory() as session:
        service = HostService(session)
        known_hosts = await service.scan_known_hosts(host)

    assert known_hosts == b"lb-sandbox-test.example.ts.net ssh-ed25519 AAAATEST\n"
    create_subprocess_exec.assert_awaited_once()
    assert create_subprocess_exec.await_args is not None
    assert create_subprocess_exec.await_args.args == (
        "ssh-keyscan",
        "-p",
        "22",
        "lb-sandbox-test.example.ts.net",
    )


async def test_scan_known_hosts_honors_external_ssh_port(monkeypatch):
    """Scans the external address on its published port, not a hardcoded 22."""
    host = Host(
        name="sb-test",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        internal_ssh_host=None,
        external_ssh_host="127.0.0.1",
        external_ssh_port=49160,
        known_hosts="",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"[127.0.0.1]:49160 ssh-ed25519 AAAA\n", b"")),
    )
    create_subprocess_exec = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "hosts.service.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )

    async with async_session_factory() as session:
        known_hosts = await HostService(session).scan_known_hosts(host)

    assert b"127.0.0.1" in known_hosts
    create_subprocess_exec.assert_awaited_once()
    assert create_subprocess_exec.await_args is not None
    assert create_subprocess_exec.await_args.args == ("ssh-keyscan", "-p", "49160", "127.0.0.1")


async def create_host_record(
    *,
    id: uuid.UUID | None = None,
    name: str,
    status: str,
    env: dict[str, str] | None = None,
    image: str | None = None,
    instance_type: str | None = None,
    disk_gb: int | None = None,
    tailscale_device_id: str | None = None,
) -> Host:
    now = utc_now()
    host = Host(
        id=id or uuid7(),
        name=name,
        status=status,
        image=image or ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        instance_type=instance_type,
        disk_gb=disk_gb,
        internal_ssh_host=f"{name}.example.ts.net",
        external_ssh_host="",
        env=env or {},
        tailscale_device_id=tailscale_device_id,
        created_at=now,
        updated_at=now,
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()
        await session.refresh(host)
    return host


async def test_expires_at_with_non_utc_offset_round_trips_as_utc():
    # SQLite drops tz offsets, so the bind-time UTC normalization must preserve
    # the instant: 2099-01-01T00:00-08:00 must read back as 08:00Z, not 00:00Z.
    offset_expires = datetime(2099, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=-8)))
    host = Host(
        id=uuid7(),
        name="sb-offset-test",
        status=HostStatus.ACTIVE.value,
        provider="exe",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        env={},
        external_ssh_host="",
        external_ssh_port=22,
        known_hosts="",
        created_at=utc_now(),
        updated_at=utc_now(),
        expires_at=offset_expires,
        last_error="",
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()

    async with async_session_factory() as session:
        reloaded = await session.get(Host, host.id)
        assert reloaded is not None
        assert reloaded.expires_at == datetime(2099, 1, 1, 8, 0, tzinfo=UTC)
