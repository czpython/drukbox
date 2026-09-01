import asyncio
import contextlib
import fcntl
import os
import pty
import shlex
import struct
import termios

from providers.base import SandboxProcess, TerminalSize
from providers.exceptions import ProviderTransportError

from .settings import DockerSbxSettings


def _set_terminal_size(descriptor: int, size: TerminalSize) -> None:
    winsize = struct.pack("HHHH", size.rows, size.columns, 0, 0)
    fcntl.ioctl(descriptor, termios.TIOCSWINSZ, winsize)


def _session_script(name: str, command: str | None, user: str) -> str:
    """Build the shell for one gateway session.

    The exec enters as root. The script makes the per-host home
    /home/<name>, moves into it, and exports HOME. For the root user it
    stops there, thus the default install keeps today's session exactly.

    For any other user it also gives that user the home and drops to the
    user with `su -m`. `-m` keeps the caller environment, thus the exported
    HOME stays the per-host home. `su` runs a PAM session, thus
    /etc/environment — the sandbox env the bootstrap writes — still reaches
    the session. The name is server-generated, thus the home path is a safe
    shell token; the user and the payload are quoted.
    """
    home = f"/home/{name}"
    payload = command if command is not None else "exec bash -l"
    prepare = f"mkdir -p {home} && cd {home} && export HOME={home}"
    if user == "root":
        return f"{prepare}\n{payload}"
    owner = shlex.quote(user)
    drop = f"exec su -m {owner} -s /bin/bash -c {shlex.quote(payload)}"
    return f"{prepare} && chown {owner} {home}\n{drop}"


class SbxExecProcess(SandboxProcess):
    """A live `sbx exec` process. A caller PTY request gets a local PTY pair,
    because the CLI refuses `-t` on pipes. The exec is a daemon session: a
    stopped sandbox wakes on open and stays awake while the process runs."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        pty_master: int | None,
        stderr_reader: asyncio.StreamReader | None,
    ) -> None:
        self._process = process
        self._reader = reader
        self._writer = writer
        self._pty_master = pty_master
        self._stderr_reader = stderr_reader

    @classmethod
    async def open(
        cls,
        name: str,
        *,
        command: str | None,
        terminal: TerminalSize | None,
    ) -> "SbxExecProcess":
        user = DockerSbxSettings().ssh_username
        argv = ["sbx", "exec", "--interactive"]
        if terminal:
            argv.append("--tty")
        argv.extend([name, "bash", "-l", "-c", _session_script(name, command, user)])
        environment = {**os.environ, "SBX_NO_TELEMETRY": "1"}

        try:
            if terminal:
                return await cls._open_with_pty(argv, environment, terminal)
            return await cls._open_with_pipes(argv, environment)
        except OSError as error:
            raise ProviderTransportError(f"sbx CLI could not be started: {error}") from error

    @classmethod
    async def _open_with_pty(
        cls,
        argv: list[str],
        environment: dict[str, str],
        terminal: TerminalSize,
    ) -> "SbxExecProcess":
        master, slave = pty.openpty()
        try:
            _set_terminal_size(slave, terminal)
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=environment,
                start_new_session=True,
            )
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        reader, writer = await _connect_pty_master(master)
        return cls(process, reader, writer, master, stderr_reader=None)

    @classmethod
    async def _open_with_pipes(
        cls,
        argv: list[str],
        environment: dict[str, str],
    ) -> "SbxExecProcess":
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stdin is not None
        return cls(
            process,
            process.stdout,
            process.stdin,
            pty_master=None,
            stderr_reader=process.stderr,
        )

    async def receive(self, max_bytes: int) -> bytes:
        try:
            return await self._reader.read(max_bytes)
        except OSError:
            # A PTY master raises EIO at the normal end of the stream.
            return b""

    async def receive_stderr(self, max_bytes: int) -> bytes:
        # A terminal merges every stream; pipe mode keeps stderr separate,
        # and the sbx wake banner arrives there.
        if self._stderr_reader is None:
            return b""
        return await self._stderr_reader.read(max_bytes)

    def send(self, data: bytes) -> None:
        self._writer.write(data)

    def send_eof(self) -> None:
        # A PTY has no end-of-input signal.
        if self._pty_master is None:
            self._writer.write_eof()

    def resize(self, size: TerminalSize) -> None:
        if self._pty_master is None:
            return
        _set_terminal_size(self._pty_master, size)

    async def wait(self) -> int:
        return await self._process.wait()

    async def aclose(self) -> None:
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
        with contextlib.suppress(OSError):
            self._writer.close()
        # No stream owns the PTY master descriptor; it leaks without this.
        if self._pty_master is not None:
            with contextlib.suppress(OSError):
                os.close(self._pty_master)
            self._pty_master = None
        await self._process.wait()


async def _connect_pty_master(master: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        os.fdopen(master, "rb", buffering=0, closefd=False),
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        os.fdopen(os.dup(master), "wb", buffering=0),
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer
