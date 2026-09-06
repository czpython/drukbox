"""The secrets proxy: a mitmproxy addon that swaps a sandbox's placeholder for
the real credential on the way out.

Run it with the official image:

    mitmdump -s /addon/swap.py --set exchange_url=http://exchange:8781

A sandbox sends its HTTPS through this proxy. For a host with a registered
secret, the proxy terminates TLS, asks the exchange for the real credential,
and swaps one header. Every other host is tunneled blind. A destination that
resolves to a loopback, private, link-local, or metadata address is refused,
so a sandbox cannot reach the exchange or the API through the proxy. Every
connection goes to the address the proxy checked, never to a second lookup.
Bodies stream in both directions.
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

# The exchange names the header the upstream reads and the credential for it.
UPSTREAM_HEADER = "X-Upstream-Header"
UPSTREAM_CREDENTIAL = "X-Upstream-Credential"
FORWARDED_HOST = "X-Forwarded-Host"
# The list of hosts with a registered secret is asked again after this long.
UPSTREAMS_TTL = 5.0
EXCHANGE_TIMEOUT = 5.0
_PLACEHOLDER_PREFIX = "drk."

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class Refused(Exception):
    """The exchange refused the placeholder for this host."""


class ExchangeUnavailable(Exception):
    """The exchange gave no answer."""


def placeholder_in(value: str) -> str | None:
    """The placeholder a header value carries.

    A bearer carries it after the scheme, and so does gh's ``token`` scheme.
    Basic carries it as the password, so the value is decoded and the password
    read. Anything else must be the placeholder itself. A placeholder inside a
    longer value never counts.
    """
    scheme, _, rest = value.partition(" ")
    candidate = value
    if scheme.lower() in ("bearer", "token"):
        candidate = rest.strip()
    elif scheme.lower() == "basic":
        try:
            _, _, candidate = base64.b64decode(rest.strip(), validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
    if candidate.startswith(_PLACEHOLDER_PREFIX) and candidate.count(".") == 3:
        return candidate
    return None


def is_reachable(addresses: set[Address]) -> bool:
    """Whether a destination may be dialed: every address is public, none is multicast."""
    return bool(addresses) and all(
        address.is_global and not address.is_multicast for address in addresses
    )


def is_name(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


class Exchange:
    """The exchange process, over HTTP."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.client = httpclient.AsyncHTTPClient()

    async def upstreams(self) -> set[str]:
        """The hosts with a registered secret."""
        answer = await self._get("/upstreams", {})
        if answer.code != 200:
            raise ExchangeUnavailable(f"upstreams answered {answer.code}")
        return set(json.loads(answer.body))

    async def authorize(self, placeholder: str, host: str) -> tuple[str, str]:
        """The header the upstream reads and the credential for it."""
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
            # raise_error=False keeps status errors quiet. A timeout or a
            # refused connection still raises.
            raise ExchangeUnavailable(str(exc)) from exc


class Swap:
    def __init__(self) -> None:
        self.exchange = Exchange("")
        self._upstreams: set[str] = set()
        self._upstreams_at = float("-inf")
        self._upstreams_known = False

    def load(self, loader) -> None:
        loader.add_option("exchange_url", str, "", "Address of the secrets exchange process.")

    def configure(self, updated: set[str]) -> None:
        if "exchange_url" in updated:
            self.exchange = Exchange(ctx.options.exchange_url)

    async def upstreams(self) -> set[str]:
        """The hosts with a registered secret, from the exchange, kept for a while.
        While the exchange gives no answer, the last list stands."""
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
        """Check every server connection, a blind tunnel included, and pin it to
        the address checked, so a name that changes its answer cannot reach a
        refused address. The name stays in the SNI for the upstream certificate."""
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
        if is_name(host) and not data.server.sni:
            data.server.sni = host

    async def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        server = data.context.server
        host = server.sni or server.address[0]
        data.ignore_connection = host not in await self.upstreams()
        if not data.ignore_connection:
            # The upstream certificate is checked against the CONNECT host,
            # whatever name the client puts in its own hello.
            server.sni = host

    async def requestheaders(self, flow: http.HTTPFlow) -> None:
        if flow.request.scheme != "https":
            if not is_reachable(await self.resolve(flow.request.host)):
                flow.response = http.Response.make(403, b"destination refused\n")
                return
        elif found := self._placeholder_header(flow):
            name, placeholder = found
            # The credential goes to the CONNECT host, which the connection
            # keeps as its SNI. The authority the client sends must agree.
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
        flow.request.stream = True

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        flow.response.stream = True

    @staticmethod
    def _placeholder_header(flow: http.HTTPFlow) -> tuple[str, str] | None:
        for name, value in flow.request.headers.items():
            if placeholder := placeholder_in(value):
                return name, placeholder
        return None


addons = [Swap()]
