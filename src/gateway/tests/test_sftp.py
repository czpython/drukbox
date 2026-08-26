import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import asyncssh
import pytest

from core.database import async_session_factory
from gateway import server as gateway_server
from gateway.settings import GatewaySettings
from gateway.tests.localprocess import SFTP_SERVER, SFTP_SERVER_COMMAND, LocalProcess
from hosts.models import Host

pytestmark = pytest.mark.skipif(
    not os.path.exists(SFTP_SERVER), reason="no local sftp-server to back the tests"
)


async def _insert_active_host(name: str, public_key: str) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        session.add(
            Host(
                name=name,
                provider="docker-sbx",
                image="template",
                status="active",
                public_key=public_key,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


@pytest.fixture
def gateway_settings(tmp_path):
    return GatewaySettings(
        ssh_host="127.0.0.1",
        ssh_port=0,
        bind_host="127.0.0.1",
        host_key_path=tmp_path / "gateway_host_key",
    )


@pytest.fixture
def local_provider(monkeypatch):
    LocalProcess.open_count = 0
    provider = SimpleNamespace(
        gateway_process_class=LocalProcess, sftp_server_command=SFTP_SERVER_COMMAND
    )
    monkeypatch.setattr(gateway_server, "get_vm_provider", lambda name: provider)
    return LocalProcess


@pytest.fixture
async def connected(gateway_settings, local_provider):
    key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host("sb-sftp", key.export_public_key().decode())
    server = await gateway_server.start(gateway_settings)
    connection = await asyncssh.connect(
        "127.0.0.1",
        server.get_port(),
        username="sb-sftp",
        client_keys=[key],
        known_hosts=None,
    )
    yield connection
    connection.close()
    server.close()


async def test_forwards_operations_to_the_sandbox(connected, tmp_path):
    # One round trip proves the delegation wiring: mkdir, write, chmod, and
    # read all reach the upstream server and come back.
    remote = tmp_path / "nested" / "artifact.bin"
    payload = b"forwarded"

    async with connected.start_sftp_client() as sftp:
        await sftp.makedirs(str(remote.parent), exist_ok=True)
        async with sftp.open(str(remote), "wb") as handle:
            await handle.write(payload)
        await sftp.chmod(str(remote), 0o600)
        async with sftp.open(str(remote), "rb") as handle:
            got = await handle.read()

    assert got == payload
    assert (remote.stat().st_mode & 0o777) == 0o600


async def test_an_operation_error_reaches_the_caller_and_keeps_the_session(connected, tmp_path):
    # An upstream error surfaces as its SFTP error, and the backend stays
    # open for the next operation.
    async with connected.start_sftp_client() as sftp:
        with pytest.raises(asyncssh.SFTPNoSuchFile):
            await sftp.stat(str(tmp_path / "not-here-yet"))
        probe = tmp_path / "probe"
        probe.write_bytes(b"ok")
        assert await sftp.isfile(str(probe))


async def test_tail_pattern_polls_without_a_per_poll_process_open(connected, tmp_path):
    # The consumer opens a fresh SFTP client per poll on one connection,
    # reads new bytes at a growing offset, and stats a marker that appears
    # later. After warmup, no poll opens a new backend process.
    transcript = tmp_path / "transcript.log"
    transcript.write_bytes(b"")
    marker = tmp_path / "done.marker"

    async with connected.start_sftp_client() as sftp:
        await sftp.stat(str(tmp_path))  # warm the backend
    await asyncio.sleep(0)
    opens_after_warmup = LocalProcess.open_count

    collected = bytearray()
    offset = 0
    for i in range(6):
        transcript.write_bytes(transcript.read_bytes() + b"line-%d\n" % i)
        if i == 5:
            marker.write_bytes(b"")
        async with connected.start_sftp_client() as sftp:
            async with sftp.open(str(transcript), "rb") as handle:
                chunk = await handle.read(-1, offset=offset)
            collected.extend(chunk)
            offset += len(chunk)
            done = await sftp.exists(str(marker))
        if done:
            break

    assert bytes(collected) == b"".join(b"line-%d\n" % i for i in range(6))
    assert LocalProcess.open_count == opens_after_warmup


async def test_concurrent_sftp_sessions_share_one_backend(connected, tmp_path):
    for i in range(4):
        (tmp_path / f"f{i}").write_bytes(b"data-%d" % i)

    async def read_one(i: int) -> bytes:
        async with (
            connected.start_sftp_client() as sftp,
            sftp.open(str(tmp_path / f"f{i}"), "rb") as handle,
        ):
            return await handle.read()

    results = await asyncio.gather(*(read_one(i) for i in range(4)))
    assert results == [b"data-%d" % i for i in range(4)]
    # Concurrent sessions multiplex over the one upstream server.
    assert LocalProcess.open_count == 1


async def test_port_forwarding_stays_refused(connected):
    # A direct-tcpip channel asks the gateway to open an outbound connection.
    # The gateway serves no forwarding, thus the request is refused.
    with pytest.raises(asyncssh.ChannelOpenError):
        await connected.open_connection("127.0.0.1", 9)
