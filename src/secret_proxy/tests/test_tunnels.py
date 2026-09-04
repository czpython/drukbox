import asyncio
import socket
import stat
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

import asyncssh
import pytest
from sqlalchemy import select

from core.database import async_session_factory
from hosts.models import Host, HostStatus
from secret_proxy.exceptions import ReverseTunnelError
from secret_proxy.settings import SecretProxySettings
from secret_proxy.tunnels import (
    TUNNEL_IDENTITY_PREFIX,
    ReverseTunnel,
    ReverseTunnelManager,
    load_reverse_tunnel_key,
)


class ForwardingSSHServer(asyncssh.SSHServer):
    def __init__(
        self,
        connections: list[asyncssh.SSHServerConnection],
        client_key: asyncssh.SSHKey,
    ) -> None:
        self.connections = connections
        self.client_key = client_key

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self.connections.append(conn)

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        return listen_host == "127.0.0.1"

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return key == self.client_key


def test_tunnel_key_rejects_group_or_world_access(tmp_path: Path) -> None:
    key_path = tmp_path / "insecure_key"
    key_path.write_bytes(asyncssh.generate_private_key("ssh-ed25519").export_private_key("openssh"))
    key_path.chmod(0o644)
    load_reverse_tunnel_key.cache_clear()

    with pytest.raises(
        ReverseTunnelError,
        match="reverse tunnel key must not be group or world accessible",
    ):
        load_reverse_tunnel_key(key_path)


async def test_tunnel_survives_an_unrelated_connection_and_reports_its_own_drop(
    tmp_path: Path,
) -> None:
    tunnel_identities: list[bytes] = []
    target_server = await asyncio.start_server(
        partial(_echo_tunnel, identities=tunnel_identities),
        "127.0.0.1",
        0,
    )
    target_port = _server_port(target_server)
    box_port = _unused_port()
    settings = SecretProxySettings.model_validate(
        {
            "bind_port": target_port,
            "tunnel_box_port": box_port,
            "tunnel_key_path": tmp_path / "tunnel_key",
            "tunnel_connect_timeout_seconds": 0.5,
            "tunnel_reconcile_interval_seconds": 0.01,
            "tunnel_keepalive_interval_seconds": 0.1,
            "tunnel_keepalive_count_max": 1,
        }
    )
    first_manager = ReverseTunnelManager(settings)
    first_process_public_key = first_manager.public_key
    assert stat.S_IMODE(settings.expanded_tunnel_key_path.stat().st_mode) == 0o600
    with pytest.raises(
        ReverseTunnelError,
        match="another API process already owns the reverse tunnels",
    ):
        ReverseTunnelManager(settings)
    await first_manager.aclose()
    load_reverse_tunnel_key.cache_clear()
    manager = ReverseTunnelManager(settings)
    assert manager.public_key == first_process_public_key
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.import_public_key(first_process_public_key)
    connections: list[asyncssh.SSHServerConnection] = []
    ssh_server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=lambda: ForwardingSSHServer(connections, client_key),
        server_host_keys=[host_key],
    )
    ssh_port = ssh_server.get_port()
    known_hosts = f"[127.0.0.1]:{ssh_port} {_public_key(host_key)}\n"
    now = datetime.now(UTC)
    host = Host(
        name="sb-tunnel-test",
        status=HostStatus.ACTIVE.value,
        provider="docker",
        image="sandbox:latest",
        external_ssh_host="127.0.0.1",
        external_ssh_port=ssh_port,
        ssh_username="root",
        known_hosts=known_hosts,
        secrets={},
        created_at=now,
        updated_at=now,
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()

    try:
        await manager.start()
        assert await _round_trip(box_port, b"first") == b"first"
        expected_identity = TUNNEL_IDENTITY_PREFIX + host.name.encode("ascii") + b"\r\n"
        assert tunnel_identities == [expected_identity]

        shared_connection = await asyncssh.connect(
            "127.0.0.1",
            port=ssh_port,
            username="root",
            known_hosts=known_hosts.encode(),
            client_keys=[settings.expanded_tunnel_key_path],
        )
        shared_connection.close()
        await shared_connection.wait_closed()

        assert await _round_trip(box_port, b"still-open") == b"still-open"
        assert tunnel_identities == [expected_identity, expected_identity]

        connections[0].abort()
        deadline = asyncio.get_running_loop().time() + 1
        while True:
            async with async_session_factory() as session:
                failed = (
                    await session.execute(select(Host).where(Host.id == host.id))
                ).scalar_one()
            if failed.status == HostStatus.ERROR.value:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("host did not reach error status")
            await asyncio.sleep(0.01)
        assert failed.last_error == (
            "ReverseTunnelError: reverse tunnel disconnected; create a replacement host"
        )
        assert failed.expires_at
    finally:
        await manager.aclose()
        ssh_server.close()
        await ssh_server.wait_closed()
        target_server.close()
        await target_server.wait_closed()


async def test_tailnet_tunnel_uses_ssh_without_a_client_key() -> None:
    tunnel_identities: list[bytes] = []
    target_server = await asyncio.start_server(
        partial(_echo_tunnel, identities=tunnel_identities),
        "127.0.0.1",
        0,
    )
    target_port = _server_port(target_server)
    box_port = _unused_port()
    settings = SecretProxySettings.model_validate(
        {
            "bind_port": target_port,
            "tunnel_box_port": box_port,
            "tunnel_connect_timeout_seconds": 0.2,
        }
    )
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    ssh_server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=NoneAuthForwardingSSHServer,
        server_host_keys=[host_key],
    )
    ssh_port = ssh_server.get_port()
    host_id = uuid.uuid4()
    tunnel = await ReverseTunnel.open(
        host_id=host_id,
        host_name="sb-tailnet-test",
        ssh_host="127.0.0.1",
        ssh_port=ssh_port,
        ssh_username="ubuntu",
        known_hosts=f"[127.0.0.1]:{ssh_port} {_public_key(host_key)}\n",
        client_key=None,
        settings=settings,
        dropped=AsyncMock(),
    )

    try:
        assert await _round_trip(box_port, b"tailnet") == b"tailnet"
        assert tunnel_identities == [TUNNEL_IDENTITY_PREFIX + b"sb-tailnet-test\r\n"]
    finally:
        await tunnel.aclose()
        ssh_server.close()
        await ssh_server.wait_closed()
        target_server.close()
        await target_server.wait_closed()


async def test_tunnel_opening_can_be_cancelled_and_is_bounded_by_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SecretProxySettings.model_validate({"tunnel_key_path": tmp_path / "tunnel_key"})
    manager = ReverseTunnelManager(settings)
    opening_started = asyncio.Event()

    async def wait_while_opening(**_kwargs: object) -> ReverseTunnel:
        opening_started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled tunnel opening continued")

    monkeypatch.setattr(ReverseTunnel, "open", wait_while_opening)
    now = datetime.now(UTC)
    host = Host(
        name="sb-opening-test",
        status=HostStatus.ACTIVE.value,
        provider="docker",
        image="sandbox:latest",
        external_ssh_host="127.0.0.1",
        external_ssh_port=22,
        ssh_username="root",
        known_hosts="host ssh-ed25519 AAAATEST\n",
        secrets={},
        created_at=now,
        updated_at=now,
    )

    opening = asyncio.create_task(manager.ensure(host, timeout_seconds=1))
    await opening_started.wait()
    await manager.close(host.id)

    with pytest.raises(asyncio.CancelledError):
        await opening
    with pytest.raises(
        ReverseTunnelError,
        match="reverse tunnel could not connect before the timeout",
    ):
        await manager.ensure(host, timeout_seconds=0.01)
    await manager.aclose()


class NoneAuthForwardingSSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        return listen_host == "127.0.0.1"


async def _echo_tunnel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    identities: list[bytes],
) -> None:
    identities.append(await reader.readline())
    writer.write(await reader.read(1024))
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def _server_port(server: asyncio.Server) -> int:
    socket_address = server.sockets[0].getsockname()
    return int(socket_address[1])


def _unused_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _public_key(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode().strip()


async def _round_trip(port: int, value: bytes) -> bytes:
    deadline = asyncio.get_running_loop().time() + 1
    while True:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.01)
            continue
        writer.write(value)
        await writer.drain()
        received = await reader.read(1024)
        writer.close()
        await writer.wait_closed()
        return received
