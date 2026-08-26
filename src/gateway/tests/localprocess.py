import asyncio
import os

from providers.base import SandboxProcess, TerminalSize

SFTP_SERVER = "/usr/libexec/sftp-server"
SFTP_SERVER_COMMAND = f"exec {SFTP_SERVER}"


class LocalProcess(SandboxProcess):
    open_count = 0
    # Per-host homes live under this root. A test points it at a writable
    # temporary directory, the way the sandbox uses /home.
    home_root = "/tmp"

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @classmethod
    async def open(cls, name, *, command, terminal):
        cls.open_count += 1
        home = os.path.join(cls.home_root, name)
        os.makedirs(home, exist_ok=True)
        argv = ["bash", "-c", command] if command else ["bash"]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=home,
            env={**os.environ, "HOME": home},
            start_new_session=True,
        )
        return cls(process)

    async def receive(self, max_bytes: int) -> bytes:
        assert self._process.stdout is not None
        return await self._process.stdout.read(max_bytes)

    async def receive_stderr(self, max_bytes: int) -> bytes:
        assert self._process.stderr is not None
        return await self._process.stderr.read(max_bytes)

    def send(self, data: bytes) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(data)

    def send_eof(self) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write_eof()

    def resize(self, size: TerminalSize) -> None:
        return

    async def wait(self) -> int:
        return await self._process.wait()

    async def aclose(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()
        await self._process.wait()
