import asyncio
import json
from contextlib import suppress
from pathlib import Path

from secret_proxy.exceptions import (
    SecretProxyRejectedError,
    SecretProxyUnavailableError,
)
from secret_proxy.settings import SecretProxySettings


class SecretProxyClient:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    @classmethod
    def from_settings(cls) -> "SecretProxyClient":
        return cls(SecretProxySettings().expanded_control_socket)

    async def put_secret(
        self,
        *,
        vm: str,
        host: str,
        env_var: str,
        headers: dict[str, str],
        placeholder: str,
        value: str,
    ) -> None:
        await self._request(
            {
                "operation": "put",
                "vm": vm,
                "host": host,
                "env_var": env_var,
                "headers": headers,
                "placeholder": placeholder,
                "value": value,
            }
        )

    async def delete_secret(self, *, vm: str, env_var: str) -> None:
        await self._request({"operation": "delete", "vm": vm, "env_var": env_var})

    async def list_secrets(self, *, vm: str) -> list[str]:
        result = await self._request({"operation": "list", "vm": vm})
        if not isinstance(result, list) or not all(isinstance(item, str) for item in result):
            raise SecretProxyUnavailableError("secret proxy returned an invalid response")
        return result

    async def route(self, *, vm: str) -> dict[str, str]:
        result = await self._request({"operation": "route", "vm": vm})
        if not isinstance(result, dict):
            raise SecretProxyUnavailableError("secret proxy returned an invalid response")
        username = result.get("username")
        password = result.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise SecretProxyUnavailableError("secret proxy returned an invalid response")
        return {"username": username, "password": password}

    async def _request(self, request: dict[str, object]) -> object:
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except OSError as error:
            raise SecretProxyUnavailableError("secret proxy is unavailable") from error

        try:
            writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
            response_line = await reader.readline()
        except OSError as error:
            raise SecretProxyUnavailableError("secret proxy request failed") from error
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

        try:
            response = json.loads(response_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SecretProxyUnavailableError(
                "secret proxy returned an invalid response"
            ) from error
        if not isinstance(response, dict):
            raise SecretProxyUnavailableError("secret proxy returned an invalid response")
        if response.get("ok") is True:
            return response.get("result")
        if response.get("error") == "rejected":
            raise SecretProxyRejectedError("secret proxy rejected the request")
        raise SecretProxyUnavailableError("secret proxy request failed")
