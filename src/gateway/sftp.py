# asyncssh types SFTP file handles as bytes on the client side and as an
# opaque object on the server side. This module bridges the two by handing
# each server handle straight to the client, so the handle-argument and
# override checks do not apply here.
# pyright: reportArgumentType=false, reportIncompatibleMethodOverride=false
import asyncssh

from gateway.backend import SandboxSftpBackend

# The SFTP protocol version drukbox speaks upstream and to callers. A stat
# with these flags asks the server for every attribute it knows.
_STAT_ALL = 0x8000_01FD


class GatewaySFTPServer(asyncssh.SFTPServer):
    """One caller SFTP session, forwarded to the sandbox's own SFTP server.

    The connection's backend holds the real upstream client. Every operation
    delegates to it. Thus the sandbox's OpenSSH server does the file work,
    and its errors — already SFTP errors — go straight back to the caller.
    Many sessions share one backend, so a per-session poll starts no new
    process.
    """

    def __init__(self, chan, backend: SandboxSftpBackend):
        super().__init__(chan)
        self._backend = backend

    async def open(self, path, pflags, attrs):
        async with self._backend.session() as handler:
            return await handler.open(path, pflags, attrs)

    async def close(self, file_obj):
        async with self._backend.session() as handler:
            await handler.close(file_obj)

    async def read(self, file_obj, offset, size):
        async with self._backend.session() as handler:
            data, _ = await handler.read(file_obj, offset, size)
            return data

    async def write(self, file_obj, offset, data):
        async with self._backend.session() as handler:
            return await handler.write(file_obj, offset, data)

    async def fstat(self, file_obj):
        async with self._backend.session() as handler:
            return await handler.fstat(file_obj, _STAT_ALL)

    async def stat(self, path):
        async with self._backend.session() as handler:
            return await handler.stat(path, _STAT_ALL)

    async def lstat(self, path):
        async with self._backend.session() as handler:
            return await handler.lstat(path, _STAT_ALL)

    async def setstat(self, path, attrs):
        async with self._backend.session() as handler:
            await handler.setstat(path, attrs)

    async def mkdir(self, path, attrs):
        async with self._backend.session() as handler:
            await handler.mkdir(path, attrs)

    async def remove(self, path):
        async with self._backend.session() as handler:
            await handler.remove(path)

    async def realpath(self, path):
        async with self._backend.session() as handler:
            names, _ = await handler.realpath(path)
            return names[0].filename

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
