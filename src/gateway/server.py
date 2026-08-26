import asyncio
import contextlib
import logging

import asyncssh
import asyncssh.sftp
from sqlalchemy import select

from core.database import async_session_factory
from gateway.backend import SandboxSftpBackend
from gateway.settings import GatewaySettings
from gateway.sftp import GatewaySFTPServer
from hosts.models import Host, HostStatus
from providers.base import SandboxProcess, TerminalSize
from providers.exceptions import ProviderError
from providers.registry import get_vm_provider

logger = logging.getLogger(__name__)

_RECEIVE_CHUNK_BYTES = 32768

# The gateway forwards each file operation to the sandbox by an opaque
# handle. Two SFTP extensions ask the server to seek inside an open file:
# server-side copy and sparse-range detection. The gateway cannot serve
# them on an opaque handle. asyncssh advertises them from this class list
# and gives no per-server control, so the gateway removes them once, before
# it serves the first SFTP session.
_UNSUPPORTED_SFTP_EXTENSIONS = (b"copy-data", b"ranges@asyncssh.com")
_extensions_disabled = False


def _disable_unsupported_sftp_extensions() -> None:
    global _extensions_disabled
    if _extensions_disabled:
        return
    asyncssh.sftp.SFTPServerHandler._extensions = [
        extension
        for extension in asyncssh.sftp.SFTPServerHandler._extensions
        if extension[0] not in _UNSUPPORTED_SFTP_EXTENSIONS
    ]
    _extensions_disabled = True


class GatewayConnection(asyncssh.SSHServer):
    """One caller connection. The key is the identity; the username must name
    the same host, so one leaked key cannot probe other host names."""

    def __init__(self) -> None:
        self.host: Host | None = None
        self._sftp_backend: SandboxSftpBackend | None = None
        self._cleanup: asyncio.Task[None] | None = None

    def sftp_backend(self) -> SandboxSftpBackend:
        """Return the connection's one SFTP backend, shared by every SFTP
        session. It is made on first use; its process opens lazily."""
        assert self.host is not None
        if self._sftp_backend is None:
            process_class = get_vm_provider(self.host.provider).gateway_process_class
            if not process_class:
                raise asyncssh.SFTPOpUnsupported("cannot open a session for this host")
            self._sftp_backend = SandboxSftpBackend(process_class, self.host.name)
        return self._sftp_backend

    def connection_lost(self, exc: Exception | None) -> None:
        # Close the backend, so its exec process ends and the sandbox can
        # sleep. Keep the task until it finishes: the loop can otherwise
        # collect it during the cleanup.
        if self._sftp_backend:
            with contextlib.suppress(RuntimeError):
                self._cleanup = asyncio.get_running_loop().create_task(self._sftp_backend.aclose())

    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    async def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Host).where(Host.name == username, Host.status == HostStatus.ACTIVE.value)
            )
            host = result.scalar_one_or_none()
        if host and host.public_key:
            try:
                stored = asyncssh.import_public_key(host.public_key)
            except asyncssh.KeyImportError:
                logger.warning("gateway: host %s has an unreadable public key", host.name)
                return False
            if stored.public_data == key.public_data:
                self.host = host
                return True
        logger.info("gateway: rejected key for username=%r", username)
        return False


async def _bridge(process: asyncssh.SSHServerProcess) -> None:
    connection = process.channel.get_connection()
    server = connection.get_owner()
    assert isinstance(server, GatewayConnection) and server.host is not None
    host = server.host

    terminal: TerminalSize | None = None
    if process.get_terminal_type():
        columns, rows, _, _ = process.get_terminal_size()
        terminal = TerminalSize(columns=columns, rows=rows)

    process_class = get_vm_provider(host.provider).gateway_process_class
    if not process_class:
        logger.warning("gateway: host %s has a direct-dial provider", host.name)
        process.stderr.write(b"cannot open a session for this host\n")
        process.exit(255)
        return
    try:
        sandbox_process = await process_class.open(
            host.name, command=process.command, terminal=terminal
        )
    except ProviderError as error:
        logger.warning("gateway: open failed for host=%s: %s", host.name, error)
        process.stderr.write(b"cannot open a session for this host\n")
        process.exit(255)
        return

    logger.info(
        "gateway: session open host=%s command=%r terminal=%s",
        host.name,
        process.command,
        terminal,
    )
    # The input pump can outlive the session (a caller who sends no EOF),
    # thus it gets a cancel, not a wait.
    input_pump = asyncio.create_task(_pump_channel_to_process(process, sandbox_process))
    stderr_pump = asyncio.create_task(_pump_stderr_to_channel(process, sandbox_process))
    try:
        while data := await sandbox_process.receive(_RECEIVE_CHUNK_BYTES):
            process.stdout.write(data)
            await process.stdout.drain()
        await stderr_pump
        process.exit(await sandbox_process.wait())
    finally:
        input_pump.cancel()
        stderr_pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await input_pump
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_pump
        await sandbox_process.aclose()
        logger.info("gateway: session closed host=%s", host.name)


async def _pump_stderr_to_channel(
    process: asyncssh.SSHServerProcess,
    sandbox_process: SandboxProcess,
) -> None:
    while data := await sandbox_process.receive_stderr(_RECEIVE_CHUNK_BYTES):
        process.stderr.write(data)
        await process.stderr.drain()


async def _pump_channel_to_process(
    process: asyncssh.SSHServerProcess,
    sandbox_process: SandboxProcess,
) -> None:
    while True:
        try:
            data = await process.stdin.read(_RECEIVE_CHUNK_BYTES)
        except asyncssh.TerminalSizeChanged as change:
            sandbox_process.resize(TerminalSize(columns=change.width, rows=change.height))
            continue
        except asyncssh.BreakReceived:
            continue
        if not data:
            sandbox_process.send_eof()
            return
        sandbox_process.send(data)


def _load_host_key(settings: GatewaySettings) -> asyncssh.SSHKey:
    path = settings.host_key_path
    if path.exists():
        return asyncssh.read_private_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519")
    path.touch(mode=0o600)
    path.write_bytes(key.export_private_key("openssh"))
    logger.info("gateway: made a new host key at %s", path)
    return key


def _open_sftp(channel: asyncssh.SSHServerChannel) -> GatewaySFTPServer:
    _disable_unsupported_sftp_extensions()
    server = channel.get_connection().get_owner()
    assert isinstance(server, GatewayConnection)
    return GatewaySFTPServer(channel, server.sftp_backend())


async def start(settings: GatewaySettings) -> asyncssh.SSHAcceptor:
    server = await asyncssh.listen(
        host=settings.bind_host,
        port=settings.ssh_port,
        server_host_keys=[_load_host_key(settings)],
        server_factory=GatewayConnection,
        process_factory=_bridge,
        encoding=None,
        allow_scp=False,
        sftp_factory=_open_sftp,
        agent_forwarding=False,
        x11_forwarding=False,
    )
    logger.info("gateway: listening on %s:%d", settings.bind_host, settings.ssh_port)
    return server


async def serve() -> None:
    server = await start(GatewaySettings())
    await server.wait_closed()


if __name__ == "__main__":
    # Service entry point: `python -m gateway.server`.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(serve())
