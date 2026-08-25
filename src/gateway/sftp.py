from typing import cast

import asyncssh

from gateway.backend import SandboxSftpBackend

# The SFTP protocol version drukbox speaks upstream and to callers. It has
# no attribute flag, so a stat asks the server for everything it knows.
_STAT_ALL = 0x8000_01FD


class GatewaySFTPServer(asyncssh.SFTPServer):
    """One caller SFTP session, forwarded to the sandbox's own SFTP server.

    The connection's backend holds the real upstream client. Every operation
    delegates to it, so the sandbox's OpenSSH server does the file work and
    its errors — already SFTP errors — pass straight back to the caller.
    Many sessions share one backend, thus a per-session poll costs no new
    process start.
    """

    def __init__(self, chan: asyncssh.SSHServerChannel, backend: SandboxSftpBackend) -> None:
        super().__init__(chan)
        self._backend = backend

    async def open(self, path: bytes, pflags: int, attrs: asyncssh.SFTPAttrs) -> bytes:
        async with self._backend.session() as handler:
            return await handler.open(path, pflags, attrs)

    async def close(self, file_obj: object) -> None:
        async with self._backend.session() as handler:
            await handler.close(cast(bytes, file_obj))

    async def read(self, file_obj: object, offset: int, size: int) -> bytes:
        async with self._backend.session() as handler:
            data, _ = await handler.read(cast(bytes, file_obj), offset, size)
            return data

    async def write(self, file_obj: object, offset: int, data: bytes) -> int:
        async with self._backend.session() as handler:
            return await handler.write(cast(bytes, file_obj), offset, data)

    async def fstat(self, file_obj: object) -> asyncssh.SFTPAttrs:
        async with self._backend.session() as handler:
            return await handler.fstat(cast(bytes, file_obj), _STAT_ALL)

    async def stat(self, path: bytes) -> asyncssh.SFTPAttrs:
        async with self._backend.session() as handler:
            return await handler.stat(path, _STAT_ALL)

    async def lstat(self, path: bytes) -> asyncssh.SFTPAttrs:
        async with self._backend.session() as handler:
            return await handler.lstat(path, _STAT_ALL)

    async def setstat(self, path: bytes, attrs: asyncssh.SFTPAttrs) -> None:
        async with self._backend.session() as handler:
            await handler.setstat(path, attrs)

    async def mkdir(self, path: bytes, attrs: asyncssh.SFTPAttrs) -> None:
        async with self._backend.session() as handler:
            await handler.mkdir(path, attrs)

    async def remove(self, path: bytes) -> None:
        async with self._backend.session() as handler:
            await handler.remove(path)

    async def realpath(self, path: bytes) -> bytes:
        async with self._backend.session() as handler:
            names, _ = await handler.realpath(path)
            return cast(bytes, names[0].filename)

    def _unsupported(self, *args, **kwargs):
        raise asyncssh.SFTPOpUnsupported("not served by this gateway")

    fsetstat = _unsupported
    scandir = _unsupported
    rmdir = _unsupported
    rename = _unsupported
    posix_rename = _unsupported
    readlink = _unsupported
    symlink = _unsupported
    link = _unsupported
    lock = _unsupported
    unlock = _unsupported
