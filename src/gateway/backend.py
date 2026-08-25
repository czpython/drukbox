import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import cast

from asyncssh import SSHReader, SSHWriter
from asyncssh.sftp import SFTPClientHandler

from providers.base import SandboxProcess

logger = logging.getLogger(__name__)

# The sandbox's own OpenSSH SFTP server. The daemon owns the data plane,
# thus file transfer runs this real server inside the sandbox and speaks
# the SFTP protocol to it, rather than reimplementing file operations.
SFTP_SERVER_COMMAND = "exec /usr/lib/openssh/sftp-server"

_SFTP_VERSION = 3

# One `sbx exec` costs seconds of CLI startup, thus the server stays open
# between operations. It closes after this idle period, so an unused
# sandbox goes back to sleep, and opens again on the next operation.
IDLE_CLOSE_SECONDS = 30.0


class _ExecReader:
    """Adapts a process byte stream to the reader the SFTP client expects:
    exact reads, a place for connection info, and a child logger."""

    def __init__(self, process: SandboxProcess) -> None:
        self._process = process
        self._buffer = bytearray()
        self.logger = _NullLogger()

    async def readexactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = await self._process.receive(count - len(self._buffer))
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(self._buffer), count)
            self._buffer.extend(chunk)
        taken = bytes(self._buffer[:count])
        del self._buffer[:count]
        return taken

    def get_extra_info(self, name: str, default=None):
        return default


class _ExecWriter:
    def __init__(self, process: SandboxProcess) -> None:
        self._process = process

    def write(self, data: bytes) -> None:
        self._process.send(data)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._process.send_eof()


class _NullLogger:
    def get_child(self, *args, **kwargs) -> "_NullLogger":
        return self

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class SandboxSftpBackend:
    """The one SFTP server behind every SFTP session on one SSH connection.

    An SFTP client drives the sandbox's own OpenSSH SFTP server over a
    persistent `sbx exec`. Sessions share it, so per-session polling costs
    no new process start. It closes after an idle period and opens again on
    the next use, so an inactive sandbox still sleeps.
    """

    def __init__(
        self,
        process_class: type[SandboxProcess],
        host_name: str,
        *,
        idle_close_seconds: float = IDLE_CLOSE_SECONDS,
    ) -> None:
        self._process_class = process_class
        self._host_name = host_name
        self._idle_close_seconds = idle_close_seconds
        self._lock = asyncio.Lock()
        self._process: SandboxProcess | None = None
        self._handler: SFTPClientHandler | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._in_flight = 0
        self._idle_closer: asyncio.Task[None] | None = None
        self._closed = False

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[SFTPClientHandler]:
        """The live SFTP handler for one operation. Opens the process on
        first use; the idle timer runs only while nothing is in flight."""
        handler = await self._acquire()
        try:
            yield handler
        finally:
            await self._release()

    async def _acquire(self) -> SFTPClientHandler:
        async with self._lock:
            if self._closed:
                raise ConnectionError("the backend is closed")
            if self._idle_closer:
                self._idle_closer.cancel()
                self._idle_closer = None
            if self._handler is None:
                await self._open()
            assert self._handler is not None
            self._in_flight += 1
            return self._handler

    async def _release(self) -> None:
        async with self._lock:
            self._in_flight -= 1
            if self._in_flight == 0 and not self._closed:
                self._idle_closer = asyncio.create_task(self._close_after_idle())

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            if self._idle_closer:
                self._idle_closer.cancel()
            await self._teardown()

    async def _open(self) -> None:
        process = await self._process_class.open(
            self._host_name, command=SFTP_SERVER_COMMAND, terminal=None
        )
        # The client handler reads exact byte counts and writes framed
        # packets; the exec adapters supply exactly that surface.
        handler = SFTPClientHandler(
            asyncio.get_running_loop(),
            "strict",
            cast(SSHReader, _ExecReader(process)),
            cast(SSHWriter, _ExecWriter(process)),
            _SFTP_VERSION,
        )
        await handler.start()
        self._process = process
        self._handler = handler
        self._recv_task = asyncio.create_task(handler.recv_packets())
        logger.info("gateway: sftp backend open host=%s", self._host_name)

    async def _close_after_idle(self) -> None:
        await asyncio.sleep(self._idle_close_seconds)
        async with self._lock:
            if self._in_flight == 0 and not self._closed:
                await self._teardown()
                logger.info("gateway: sftp backend idle-closed host=%s", self._host_name)

    async def _teardown(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
            self._recv_task = None
        if self._process:
            with contextlib.suppress(Exception):
                await self._process.aclose()
            self._process = None
        self._handler = None
