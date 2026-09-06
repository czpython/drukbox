"""A mitmproxy addon: swaps a sandbox's placeholder for the real credential.

    mitmdump -s /addon/swap.py --set exchange_url=http://exchange:8781

TLS is terminated for hosts with a registered secret only. A loopback,
private, link-local, or metadata destination is refused, for HTTP and CONNECT.
"""

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import socket
import time
import urllib.parse

from mitmproxy import ctx, http, tls
from mitmproxy.proxy import server_hooks
from tornado import httpclient

logger = logging.getLogger(__name__)

UPSTREAM_HEADER = "X-Upstream-Header"
UPSTREAM_CREDENTIAL = "X-Upstream-Credential"
FORWARDED_HOST = "X-Forwarded-Host"
# How long the upstream list stands.
UPSTREAMS_TTL = 5.0
EXCHANGE_TIMEOUT = 5.0
_PLACEHOLDER_PREFIX = "drk."

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class Refused(Exception):
    """The exchange refused the placeholder for this host."""


class ExchangeUnavailable(Exception):
    """The exchange gave no answer."""


def placeholder_in(value: str) -> str:
    """Basic carries it as the password. Inside a longer value it never counts."""
    scheme, _, rest = value.partition(" ")
    candidate = value
    if scheme.lower() == "bearer":
        candidate = rest.strip()
    elif scheme.lower() == "basic":
        try:
            _, _, candidate = base64.b64decode(rest.strip(), validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            candidate = ""
    if candidate.startswith(_PLACEHOLDER_PREFIX) and candidate.count(".") == 3:
        return candidate
    return ""


def is_reachable(addresses: set[Address]) -> bool:
    """Every address is public and none is multicast."""
    return bool(addresses) and all(
        address.is_global and not address.is_multicast for address in addresses
    )


class Exchange:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.client = httpclient.AsyncHTTPClient()

    async def upstreams(self) -> set[str]:
        answer = await self._get("/upstreams", {})
        if answer.code != 200:
            raise ExchangeUnavailable(f"upstreams answered {answer.code}")
        return set(json.loads(answer.body))

    async def authorize(self, placeholder: str, host: str) -> tuple[str, str]:
        answer = await self._get(
            "/authorize", {"Authorization": f"Bearer {placeholder}", FORWARDED_HOST: host}
        )
        if answer.code == 403:
            raise Refused(host)
        if answer.code != 200:
            raise ExchangeUnavailable(f"authorize answered {answer.code}")
        return answer.headers[UPSTREAM_HEADER], answer.headers[UPSTREAM_CREDENTIAL]

    async def _get(self, path: str, headers: dict[str, str]) -> httpclient.HTTPResponse:
        request = httpclient.HTTPRequest(
            f"{self.url}{path}", headers=headers, request_timeout=EXCHANGE_TIMEOUT
        )
        try:
            return await self.client.fetch(request, raise_error=False)
        except (httpclient.HTTPClientError, OSError) as exc:
            # A status is an answer. A timeout or a refused connection is not.
            raise ExchangeUnavailable(str(exc)) from exc


class Swap:
    def __init__(self) -> None:
        self.exchange: Exchange
        self._upstreams: set[str] = set()
        self._upstreams_at = float("-inf")
        self._upstreams_known = False

    def load(self, loader) -> None:
        loader.add_option("exchange_url", str, "", "Address of the secrets exchange process.")

    def configure(self, updated: set[str]) -> None:
        if "exchange_url" in updated:
            self.exchange = Exchange(ctx.options.exchange_url)

    async def upstreams(self) -> set[str]:
        """While the exchange gives no answer, the last list stands."""
        if time.monotonic() - self._upstreams_at > UPSTREAMS_TTL:
            try:
                self._upstreams = await self.exchange.upstreams()
                self._upstreams_known = True
            except ExchangeUnavailable as exc:
                logger.warning("the exchange gave no upstreams: %s", exc)
            self._upstreams_at = time.monotonic()
        return self._upstreams

    async def resolve(self, host: str) -> set[Address]:
        try:
            found = await asyncio.get_running_loop().getaddrinfo(host, 0, type=socket.SOCK_STREAM)
        except OSError:
            return set()
        return {ipaddress.ip_address(entry[4][0]) for entry in found}

    async def http_connect(self, flow: http.HTTPFlow) -> None:
        if not is_reachable(await self.resolve(flow.request.host)):
            flow.response = http.Response.make(403, b"destination refused\n")

    async def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        """Pin every connection, a blind tunnel included, to the address checked.
        The name stays in the SNI."""
        await self.upstreams()
        if not self._upstreams_known:
            data.server.error = "the exchange has not answered yet"
            return
        host, port = data.server.address
        addresses = await self.resolve(host)
        if not is_reachable(addresses):
            data.server.error = "destination refused"
            return
        chosen = min(addresses, key=lambda address: (address.version, address.packed))
        data.server.address = (str(chosen), port)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            data.server.sni = data.server.sni or host

    async def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        server = data.context.server
        host = server.sni or server.address[0]
        intercepted = host in await self.upstreams()
        data.ignore_connection = not intercepted
        if intercepted:
            # The upstream certificate is checked against the CONNECT host.
            server.sni = host

    async def requestheaders(self, flow: http.HTTPFlow) -> None:
        if flow.request.scheme != "https":
            if not is_reachable(await self.resolve(flow.request.host)):
                flow.response = http.Response.make(403, b"destination refused\n")
        else:
            for name, value in flow.request.headers.items():
                if placeholder := placeholder_in(value):
                    await self.swap(flow, name, placeholder)
                    break
        flow.request.stream = not flow.response

    async def swap(self, flow: http.HTTPFlow, name: str, placeholder: str) -> None:
        # The authority the client sends must match the CONNECT host.
        approved = flow.server_conn.sni or flow.request.host
        authority = urllib.parse.urlsplit(f"//{flow.request.host_header or ''}").hostname
        if authority != approved.lower():
            flow.response = http.Response.make(403, b"host does not match the connection\n")
            return
        try:
            header, credential = await self.exchange.authorize(placeholder, approved)
        except Refused:
            logger.info("refused a placeholder for %s", approved)
            flow.response = http.Response.make(403, b"placeholder refused\n")
            return
        except ExchangeUnavailable as exc:
            logger.warning("the exchange gave no answer for %s: %s", approved, exc)
            flow.response = http.Response.make(503, b"exchange unavailable\n")
            return
        del flow.request.headers[name]
        flow.request.headers[header] = credential

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        flow.response.stream = True


addons = [Swap()]
