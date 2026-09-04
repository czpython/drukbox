import asyncio
import json
import os
import stat
from contextlib import suppress
from pathlib import Path

from secret_proxy.exceptions import SecretProxyRejectedError, SecretProxyUnavailableError
from secret_proxy.rules import SecretRules


class SecretProxyControlServer:
    def __init__(self, socket_path: Path, rules: SecretRules, *, ca_certificate: str) -> None:
        self.socket_path = socket_path
        self.rules = rules
        self.ca_certificate = ca_certificate
        self._server: asyncio.AbstractServer | None = None
        self._socket_inode: int | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.socket_path.exists():
            mode = self.socket_path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise SecretProxyUnavailableError(
                    "secret proxy control path exists and is not a socket"
                )
            try:
                _, writer = await asyncio.open_unix_connection(self.socket_path)
            except ConnectionRefusedError:
                self.socket_path.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise SecretProxyUnavailableError("secret proxy control socket is in use")

        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=self.socket_path,
                limit=1024 * 1024,
            )
        except OSError as error:
            raise SecretProxyUnavailableError(
                "secret proxy control socket could not start"
            ) from error
        os.chmod(self.socket_path, 0o600)
        self._socket_inode = self.socket_path.stat().st_ino

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if (
            self._socket_inode
            and self.socket_path.exists()
            and self.socket_path.stat().st_ino == self._socket_inode
        ):
            self.socket_path.unlink()
        self._socket_inode = None

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        response: dict[str, object]
        try:
            request = json.loads(await reader.readline())
            result = await self._dispatch(request)
        except SecretProxyRejectedError:
            response = {"ok": False, "error": "rejected"}
        except (
            asyncio.LimitOverrunError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            response = {"ok": False, "error": "invalid_request"}
        except Exception:
            response = {"ok": False, "error": "internal_error"}
        else:
            response = {"ok": True, "result": result}
        writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _dispatch(self, request: object) -> object:
        if not isinstance(request, dict):
            raise TypeError
        operation = request["operation"]
        if operation == "certificate_authority":
            return self.ca_certificate
        vm = request["vm"]
        if not isinstance(operation, str) or not isinstance(vm, str):
            raise TypeError

        if operation == "put":
            host = request["host"]
            name = request["name"]
            placeholder = request["placeholder"]
            value = request["value"]
            if not all(isinstance(item, str) for item in (host, name, placeholder, value)):
                raise TypeError
            await self.rules.put(
                vm=vm,
                name=name,
                host=host,
                placeholder=placeholder,
                value=value,
            )
            return
        if operation == "delete":
            name = request["name"]
            if not isinstance(name, str):
                raise TypeError
            self.rules.delete(vm=vm, name=name)
            return
        if operation == "list":
            return self.rules.names(vm=vm)
        raise SecretProxyRejectedError("unknown secret proxy operation")
