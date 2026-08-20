import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar

import asyncssh
import pytest

from core.database import async_session_factory
from gateway import server as gateway_server
from gateway.settings import GatewaySettings
from hosts.models import Host, HostStatus
from providers.base import SandboxProcess, TerminalSize
from providers.exceptions import ProviderTransportError


class FakeProcess(SandboxProcess):
    """Consumes caller input until EOF, echoes one payload, exits with 7."""

    opened: ClassVar[list["FakeProcess"]] = []

    @classmethod
    async def open(cls, name, *, command, terminal):
        fake = cls(command=command, terminal=terminal)
        cls.opened.append(fake)
        return fake

    def __init__(self, *, command: str | None, terminal: TerminalSize | None) -> None:
        self.command = command
        self.terminal = terminal
        self.sent = bytearray()
        self._caller_done = asyncio.Event()
        self._announced = False

    async def receive(self, max_bytes: int) -> bytes:
        if self._announced:
            await self._caller_done.wait()
            return b""
        self._announced = True
        return f"ran:{self.command or 'shell'}".encode()

    async def receive_stderr(self, max_bytes: int) -> bytes:
        return b""

    def send(self, data: bytes) -> None:
        self.sent.extend(data)
        # A newline ends the fake, the way "exit" ends a shell. PTY callers
        # end sessions this way; they send no stdin EOF.
        if b"\n" in data:
            self._caller_done.set()

    def send_eof(self) -> None:
        self._caller_done.set()

    def resize(self, size: TerminalSize) -> None:
        self.terminal = size

    async def wait(self) -> int:
        return 7

    async def aclose(self) -> None:
        self._caller_done.set()


async def _insert_active_host(name: str, public_key: str, status: str = "active") -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        session.add(
            Host(
                name=name,
                provider="docker-sbx",
                image="template",
                status=status,
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
def fake_provider(monkeypatch):
    FakeProcess.opened.clear()
    provider = SimpleNamespace(gateway_process_class=FakeProcess)
    monkeypatch.setattr(gateway_server, "get_vm_provider", lambda name: provider)
    return FakeProcess


async def test_gateway_runs_a_command_and_returns_the_exit_status(gateway_settings, fake_provider):
    caller_key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host("sb-gwtest", caller_key.export_public_key().decode())

    server = await gateway_server.start(gateway_settings)
    try:
        async with asyncssh.connect(
            "127.0.0.1",
            server.get_port(),
            username="sb-gwtest",
            client_keys=[caller_key],
            known_hosts=None,
        ) as connection:
            result = await connection.run("uname -a", input="fed-to-session")
    finally:
        server.close()

    assert result.exit_status == 7
    assert result.stdout == "ran:uname -a"
    assert bytes(fake_provider.opened[0].sent) == b"fed-to-session"


async def test_gateway_rejects_a_key_that_is_not_the_hosts_key(gateway_settings, fake_provider):
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host("sb-gwtest", host_key.export_public_key().decode())
    attacker_key = asyncssh.generate_private_key("ssh-ed25519")

    server = await gateway_server.start(gateway_settings)
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1",
                server.get_port(),
                username="sb-gwtest",
                client_keys=[attacker_key],
                known_hosts=None,
            )
    finally:
        server.close()
    assert fake_provider.opened == []


async def test_gateway_rejects_the_right_key_under_another_hosts_name(
    gateway_settings, fake_provider
):
    # One leaked key must not open sessions on other hosts: the username and
    # the key must name the same host.
    caller_key = asyncssh.generate_private_key("ssh-ed25519")
    other_key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host("sb-mine", caller_key.export_public_key().decode())
    await _insert_active_host("sb-other", other_key.export_public_key().decode())

    server = await gateway_server.start(gateway_settings)
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1",
                server.get_port(),
                username="sb-other",
                client_keys=[caller_key],
                known_hosts=None,
            )
    finally:
        server.close()


async def test_gateway_rejects_hosts_that_are_not_active(gateway_settings, fake_provider):
    caller_key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host(
        "sb-gone",
        caller_key.export_public_key().decode(),
        status=HostStatus.ERROR.value,
    )

    server = await gateway_server.start(gateway_settings)
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1",
                server.get_port(),
                username="sb-gone",
                client_keys=[caller_key],
                known_hosts=None,
            )
    finally:
        server.close()


async def test_gateway_passes_the_callers_terminal_to_the_session(gateway_settings, fake_provider):
    caller_key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host("sb-pty", caller_key.export_public_key().decode())

    server = await gateway_server.start(gateway_settings)
    try:
        async with asyncssh.connect(
            "127.0.0.1",
            server.get_port(),
            username="sb-pty",
            client_keys=[caller_key],
            known_hosts=None,
        ) as connection:
            result = await connection.run(
                term_type="xterm", term_size=(121, 43), command=None, input="exit\n"
            )
    finally:
        server.close()

    assert result.exit_status == 7
    session = fake_provider.opened[0]
    assert session.command is None
    assert session.terminal == TerminalSize(columns=121, rows=43)


async def test_gateway_routes_the_sessions_error_stream_to_ssh_stderr(
    gateway_settings, monkeypatch
):
    class NoisyProcess(FakeProcess):
        opened: ClassVar[list[FakeProcess]] = []

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._warned = False

        async def receive_stderr(self, max_bytes: int) -> bytes:
            if self._warned:
                return b""
            self._warned = True
            return b"sandbox woke up\n"

    provider = SimpleNamespace(gateway_process_class=NoisyProcess)
    monkeypatch.setattr(gateway_server, "get_vm_provider", lambda name: provider)
    caller_key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host("sb-noisy", caller_key.export_public_key().decode())

    server = await gateway_server.start(gateway_settings)
    try:
        async with asyncssh.connect(
            "127.0.0.1",
            server.get_port(),
            username="sb-noisy",
            client_keys=[caller_key],
            known_hosts=None,
        ) as connection:
            result = await connection.run("true", input="done\n")
    finally:
        server.close()

    # Wake banners and warnings must not pollute the command's output.
    assert result.stdout == "ran:true"
    assert "sandbox woke up" in str(result.stderr)


async def test_gateway_reports_a_failed_dial_and_exits(gateway_settings, monkeypatch):
    # A stopped daemon or a vanished sandbox must end the connection with a
    # clear message and status 255, not a closed channel.
    class FailingProcess(FakeProcess):
        @classmethod
        async def open(cls, name, *, command, terminal):
            raise ProviderTransportError("daemon unavailable")

    provider = SimpleNamespace(gateway_process_class=FailingProcess)
    monkeypatch.setattr(gateway_server, "get_vm_provider", lambda name: provider)
    caller_key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host("sb-nodial", caller_key.export_public_key().decode())

    server = await gateway_server.start(gateway_settings)
    try:
        async with asyncssh.connect(
            "127.0.0.1",
            server.get_port(),
            username="sb-nodial",
            client_keys=[caller_key],
            known_hosts=None,
        ) as connection:
            result = await connection.run("uname -a")
    finally:
        server.close()

    assert result.exit_status == 255
    assert "cannot open a session" in str(result.stderr)


async def test_gateway_refuses_sftp(gateway_settings, fake_provider):
    caller_key = asyncssh.generate_private_key("ssh-ed25519")
    await _insert_active_host("sb-nosftp", caller_key.export_public_key().decode())

    server = await gateway_server.start(gateway_settings)
    try:
        async with asyncssh.connect(
            "127.0.0.1",
            server.get_port(),
            username="sb-nosftp",
            client_keys=[caller_key],
            known_hosts=None,
        ) as connection:
            with pytest.raises((asyncssh.SFTPError, asyncssh.ChannelOpenError)):
                await connection.start_sftp_client()
    finally:
        server.close()
