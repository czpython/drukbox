import asyncio
import base64
import binascii
import ipaddress
import re
import socket
import ssl
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from typing import cast
from urllib.parse import urlsplit

import aiohttp
import h11
from aiohttp.abc import AbstractResolver, ResolveResult

from secret_proxy.certificates import CertificateAuthority
from secret_proxy.control import SecretProxyControlServer
from secret_proxy.exceptions import SecretProxyRejectedError
from secret_proxy.rules import ROUTING_HEADERS, SecretRules
from secret_proxy.settings import SecretProxySettings

_REQUEST_HEADERS_TO_REMOVE = frozenset(name.encode() for name in ROUTING_HEADERS) | {
    b"authorization",
    b"cookie",
}
_RESPONSE_HEADERS_TO_REMOVE = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)


class PinnedResolver(AbstractResolver):
    def __init__(self, hostname: str, addresses: list[str]) -> None:
        self.hostname = hostname
        self.addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        return [
            ResolveResult(
                hostname=self.hostname,
                host=address,
                port=port,
                family=(
                    socket.AF_INET6
                    if ipaddress.ip_address(address).version == 6
                    else socket.AF_INET
                ),
                proto=socket.IPPROTO_TCP,
                flags=0,
            )
            for address in self.addresses
        ]

    async def close(self) -> None:
        return


class PlaceholderSubstitution:
    def __init__(self, rules: list[dict[str, object]]) -> None:
        replacements = {
            str(rule["placeholder"]).encode(): str(rule["value"]).encode() for rule in rules
        }
        self.replacements = replacements
        patterns = sorted(replacements, key=len, reverse=True)
        self.pattern = re.compile(b"|".join(re.escape(pattern) for pattern in patterns))
        self.max_pattern_length = max(map(len, patterns))
        self.pending = b""

    def replace(self, data: bytes, *, final: bool = False) -> bytes:
        self.pending += data
        if final:
            safe_limit = len(self.pending)
        else:
            safe_limit = max(0, len(self.pending) - self.max_pattern_length + 1)

        output = bytearray()
        consumed = 0
        for match in self.pattern.finditer(self.pending):
            if match.start() >= safe_limit:
                break
            output.extend(self.pending[consumed : match.start()])
            output.extend(self.replacements[match.group()])
            consumed = match.end()
        cut = max(consumed, safe_limit)
        output.extend(self.pending[consumed:cut])
        self.pending = self.pending[cut:]
        return bytes(output)


class SecretProxyServer:
    def __init__(
        self,
        settings: SecretProxySettings,
        *,
        upstream_ssl: ssl.SSLContext | bool = True,
    ) -> None:
        self.settings = settings
        self.rules = SecretRules(
            allow_private_upstreams=settings.allow_private_upstreams,
        )
        self.certificates = CertificateAuthority(settings.expanded_certificate_directory)
        self.control = SecretProxyControlServer(settings.expanded_control_socket, self.rules)
        self.upstream_ssl = upstream_ssl
        self._server: asyncio.Server | None = None

    @property
    def address(self) -> tuple[str, int]:
        if not self._server or not self._server.sockets:
            raise RuntimeError("secret proxy is not active")
        host, port, *_ = self._server.sockets[0].getsockname()
        return str(host), int(port)

    async def start(self) -> None:
        await self.control.start()
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                host=self.settings.bind_host,
                port=self.settings.bind_port,
                limit=1024 * 1024,
            )
        except Exception:
            await self.control.close()
            raise

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.control.close()

    async def serve(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.close()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            vm, authority = await self._accept_tunnel(reader, writer)
            hostname, _ = self.rules.split_host(authority)
            await writer.start_tls(self.certificates.server_context(hostname))
            await self._serve_tunnel(vm, authority, reader, writer)
        except (SecretProxyRejectedError, h11.ProtocolError, UnicodeError, ValueError):
            if not writer.is_closing():
                await self._send_plain_error(writer, 403, b"Forbidden")
        except (TimeoutError, OSError, aiohttp.ClientError, ssl.SSLError):
            if not writer.is_closing():
                await self._send_plain_error(writer, 502, b"Bad Gateway")
        except Exception:
            if not writer.is_closing():
                await self._send_plain_error(writer, 502, b"Bad Gateway")
        finally:
            writer.close()
            with suppress(OSError, ssl.SSLError):
                await writer.wait_closed()

    async def _accept_tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> tuple[str, str]:
        connection = h11.Connection(h11.SERVER)
        event = await self._next_event(connection, reader)
        if not isinstance(event, h11.Request) or event.method != b"CONNECT":
            raise SecretProxyRejectedError("secret proxy accepts CONNECT requests only")
        authority = event.target.decode("ascii")
        vm, token = self._proxy_credentials(event.headers)
        if not self.rules.authenticate(vm=vm, token=token):
            raise SecretProxyRejectedError("secret proxy authentication failed")
        if not self.rules.for_host(vm=vm, host=authority):
            raise SecretProxyRejectedError("secret proxy route is not registered")

        event = await self._next_event(connection, reader)
        if not isinstance(event, h11.EndOfMessage):
            raise SecretProxyRejectedError("CONNECT request body is not permitted")
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        return vm, authority

    async def _serve_tunnel(
        self,
        vm: str,
        authority: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        connection = h11.Connection(h11.SERVER)
        while True:
            event = await self._next_event(connection, reader)
            if isinstance(event, h11.ConnectionClosed):
                return
            if not isinstance(event, h11.Request):
                raise SecretProxyRejectedError("invalid request in secret proxy tunnel")
            await self._forward_request(vm, authority, event, connection, reader, writer)
            if connection.our_state is h11.MUST_CLOSE:
                return
            connection.start_next_cycle()

    async def _forward_request(
        self,
        vm: str,
        authority: str,
        request: h11.Request,
        connection: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        target = request.target.decode("ascii")
        parsed_target = urlsplit(target)
        if parsed_target.scheme or parsed_target.netloc or not target.startswith("/"):
            raise SecretProxyRejectedError("secret proxy request target is invalid")
        if request.method == b"CONNECT":
            raise SecretProxyRejectedError("nested CONNECT requests are not permitted")
        rules = self.rules.for_host(vm=vm, host=authority)
        if not rules:
            raise SecretProxyRejectedError("secret proxy route is not registered")
        hostname, port = self.rules.split_host(authority)
        addresses = await self.rules.resolve(hostname, port)
        headers = self._upstream_headers(request.headers, rules)
        body = self._request_body(connection, reader, PlaceholderSubstitution(rules))
        connector = aiohttp.TCPConnector(
            resolver=PinnedResolver(hostname, addresses),
            use_dns_cache=False,
            force_close=True,
        )
        timeout = aiohttp.ClientTimeout(total=self.settings.upstream_timeout_seconds)
        url = f"https://{authority}{target}"
        async with (
            aiohttp.ClientSession(connector=connector, timeout=timeout) as client,
            client.request(
                request.method.decode("ascii"),
                url,
                headers=headers,
                data=body,
                allow_redirects=False,
                auto_decompress=False,
                ssl=self.upstream_ssl,
            ) as response,
        ):
            response_headers = [
                (name, value)
                for name, value in response.raw_headers
                if name.lower() not in _RESPONSE_HEADERS_TO_REMOVE
            ]
            await self._send_event(
                connection,
                writer,
                h11.Response(status_code=response.status, headers=response_headers),
            )
            async for chunk in response.content.iter_chunked(64 * 1024):
                await self._send_event(connection, writer, h11.Data(data=chunk))
            await self._send_event(connection, writer, h11.EndOfMessage())

    async def _request_body(
        self,
        connection: h11.Connection,
        reader: asyncio.StreamReader,
        substitution: PlaceholderSubstitution,
    ) -> AsyncIterator[bytes]:
        while True:
            event = await self._next_event(connection, reader)
            if isinstance(event, h11.Data):
                if data := substitution.replace(event.data):
                    yield data
                continue
            if isinstance(event, h11.EndOfMessage):
                if data := substitution.replace(b"", final=True):
                    yield data
                return
            raise SecretProxyRejectedError("secret proxy request body is invalid")

    @staticmethod
    def _upstream_headers(
        incoming: Sequence[tuple[bytes, bytes]],
        rules: list[dict[str, object]],
    ) -> list[tuple[str, str]]:
        connection_headers: set[bytes] = set()
        for name, value in incoming:
            if name.lower() == b"connection":
                connection_headers.update(item.strip().lower() for item in value.split(b","))
        removed = _REQUEST_HEADERS_TO_REMOVE | connection_headers
        replacements = {str(rule["placeholder"]): str(rule["value"]) for rule in rules}
        headers: list[tuple[str, str]] = []
        for name, value in incoming:
            if name.lower() not in removed:
                rendered = value.decode("latin-1")
                for placeholder, secret in replacements.items():
                    rendered = rendered.replace(placeholder, secret)
                headers.append((name.decode("ascii"), rendered))
        configured: dict[str, str] = {}
        for rule in rules:
            rule_headers = cast(dict[str, str], rule["headers"])
            for name, value in rule_headers.items():
                rendered = str(value)
                for placeholder, secret in replacements.items():
                    rendered = rendered.replace(placeholder, secret)
                configured[str(name)] = rendered
        configured_names = {name.lower() for name in configured}
        headers = [(name, value) for name, value in headers if name.lower() not in configured_names]
        headers.extend(configured.items())
        return headers

    @staticmethod
    def _proxy_credentials(headers: Sequence[tuple[bytes, bytes]]) -> tuple[str, str]:
        values = [value for name, value in headers if name.lower() == b"proxy-authorization"]
        if len(values) != 1 or values[0][:6].lower() != b"basic ":
            raise SecretProxyRejectedError("proxy authorization is required")
        try:
            decoded = base64.b64decode(values[0][6:], validate=True).decode("utf-8")
            vm, token = decoded.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError) as error:
            raise SecretProxyRejectedError("proxy authorization is invalid") from error
        return vm, token

    @staticmethod
    async def _next_event(
        connection: h11.Connection,
        reader: asyncio.StreamReader,
    ) -> h11.Event:
        while True:
            event = connection.next_event()
            if event is not h11.NEED_DATA:
                if event is h11.PAUSED:
                    raise h11.LocalProtocolError("secret proxy connection is paused")
                return cast(h11.Event, event)
            data = await reader.read(64 * 1024)
            connection.receive_data(data)

    @staticmethod
    async def _send_event(
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        event: h11.Event,
    ) -> None:
        data = connection.send(event)
        if data:
            writer.write(data)
            await writer.drain()

    @staticmethod
    async def _send_plain_error(
        writer: asyncio.StreamWriter,
        status: int,
        reason: bytes,
    ) -> None:
        writer.write(
            b"HTTP/1.1 "
            + str(status).encode()
            + b" "
            + reason
            + b"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        )
        with suppress(OSError, ssl.SSLError):
            await writer.drain()
