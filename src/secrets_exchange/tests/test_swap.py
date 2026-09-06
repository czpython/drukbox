"""The proxy addon, deploy/proxy/swap.py, without mitmproxy.

mitmproxy needs a newer Python than this project, so the addon is loaded
with stand-ins for the few mitmproxy and tornado names it imports. The
acceptance run covers the addon inside the real proxy.
"""

import base64
import importlib.util
import ipaddress
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

ADDON = Path(__file__).parents[3] / "deploy" / "proxy" / "swap.py"
PLACEHOLDER = "drk.0123abcd.anthropic.s3cret"


class FakeRequest:
    def __init__(self, url: str, headers: dict[str, str], request_timeout: float) -> None:
        self.url = url
        self.headers = headers
        self.request_timeout = request_timeout


class FakeResponse:
    def __init__(self, status_code: int, content: bytes, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers

    @classmethod
    def make(
        cls, status_code: int = 200, content: bytes = b"", headers: dict[str, str] | None = None
    ) -> "FakeResponse":
        return cls(status_code, content, headers or {})


def _stand_ins() -> dict[str, ModuleType]:
    mitmproxy = ModuleType("mitmproxy")
    mitmproxy.ctx = SimpleNamespace(options=SimpleNamespace(exchange_url="http://exchange.test"))  # type: ignore[attr-defined]
    http = ModuleType("mitmproxy.http")
    http.Response = FakeResponse  # type: ignore[attr-defined]
    http.HTTPFlow = object  # type: ignore[attr-defined]
    tls = ModuleType("mitmproxy.tls")
    tls.ClientHelloData = object  # type: ignore[attr-defined]
    proxy = ModuleType("mitmproxy.proxy")
    server_hooks = ModuleType("mitmproxy.proxy.server_hooks")
    server_hooks.ServerConnectionHookData = object  # type: ignore[attr-defined]
    tornado = ModuleType("tornado")
    httpclient = ModuleType("tornado.httpclient")
    httpclient.AsyncHTTPClient = SimpleNamespace  # type: ignore[attr-defined]
    httpclient.HTTPRequest = FakeRequest  # type: ignore[attr-defined]
    httpclient.HTTPResponse = object  # type: ignore[attr-defined]
    httpclient.HTTPClientError = type("HTTPClientError", (Exception,), {})  # type: ignore[attr-defined]
    return {
        "mitmproxy": mitmproxy,
        "mitmproxy.http": http,
        "mitmproxy.tls": tls,
        "mitmproxy.proxy": proxy,
        "mitmproxy.proxy.server_hooks": server_hooks,
        "tornado": tornado,
        "tornado.httpclient": httpclient,
    }


@pytest.fixture(scope="module")
def swap() -> Iterator[ModuleType]:
    stand_ins = _stand_ins()
    sys.modules.update(stand_ins)
    try:
        spec = importlib.util.spec_from_file_location("swap", ADDON)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in stand_ins:
            sys.modules.pop(name, None)


class FakeExchange:
    """Answers like the exchange process. Records what it was asked."""

    def __init__(
        self,
        swap: ModuleType,
        upstreams: set[str],
        answers: dict[tuple[str, str], tuple[str, str]],
    ) -> None:
        self.swap = swap
        self.hosts = upstreams
        self.answers = answers
        self.asked: list[tuple[str, str]] = []
        self.upstreams_calls = 0
        self.unavailable = False

    async def upstreams(self) -> set[str]:
        self.upstreams_calls += 1
        if self.unavailable:
            raise self.swap.ExchangeUnavailable("down")
        return set(self.hosts)

    async def authorize(self, placeholder: str, host: str) -> tuple[str, str]:
        self.asked.append((placeholder, host))
        if self.unavailable:
            raise self.swap.ExchangeUnavailable("down")
        if (placeholder, host) not in self.answers:
            raise self.swap.Refused(host)
        return self.answers[(placeholder, host)]


def _flow(scheme: str, host: str, headers: dict[str, str], connect_host: str = "") -> Any:
    """A request as mitmproxy hands it over: after CONNECT the request host is
    the pinned address, the authority is the client's Host, and the CONNECT host
    is the SNI of the server connection."""
    connect_host = connect_host or host
    return SimpleNamespace(
        request=SimpleNamespace(
            scheme=scheme,
            host="104.18.0.1" if scheme == "https" else host,
            host_header=f"{host}:443" if scheme == "https" else host,
            port=443,
            headers=headers,
            stream=False,
        ),
        server_conn=SimpleNamespace(sni=connect_host if scheme == "https" else None),
        response=None,
    )


def _swap(swap: ModuleType, exchange: FakeExchange, *addresses: str) -> Any:
    addon = swap.Swap()
    addon.exchange = exchange
    addon.resolve = AsyncMock(return_value={ipaddress.ip_address(a) for a in addresses})
    return addon


def _server(host: str, port: int = 443) -> Any:
    return SimpleNamespace(server=SimpleNamespace(address=(host, port), sni=None, error=None))


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.mark.parametrize(
    ("value", "placeholder"),
    [
        (f"Bearer {PLACEHOLDER}", PLACEHOLDER),
        (f"bearer  {PLACEHOLDER}", PLACEHOLDER),
        (PLACEHOLDER, PLACEHOLDER),
        (_basic("x-access-token", PLACEHOLDER), PLACEHOLDER),
        ("Bearer sk-ant-oat01-real", None),
        (f"Bearer x{PLACEHOLDER}", None),
        (f"token {PLACEHOLDER}", None),
        ("Bearer drk.0123abcd.anthropic", None),
        (_basic("user", "ghs_real"), None),
        ("Basic not-base64!", None),
        (f"Basic {base64.b64encode(PLACEHOLDER.encode()).decode()}", None),
    ],
)
def test_a_placeholder_is_read_from_a_bearer_a_basic_password_or_a_bare_value(
    swap: ModuleType, value: str, placeholder: str | None
) -> None:
    assert swap.placeholder_in(value) == placeholder


@pytest.mark.parametrize(
    ("addresses", "reachable"),
    [
        ({"8.8.8.8"}, True),
        ({"2606:4700::1"}, True),
        ({"8.8.8.8", "127.0.0.1"}, False),
        ({"10.1.2.3"}, False),
        ({"169.254.169.254"}, False),
        ({"100.92.230.15"}, False),
        ({"fd00::1"}, False),
        ({"224.0.0.1"}, False),
        (set(), False),
    ],
)
def test_only_a_destination_with_public_addresses_is_reachable(
    swap: ModuleType, addresses: set[str], reachable: bool
) -> None:
    assert swap.is_reachable({ipaddress.ip_address(a) for a in addresses}) is reachable


async def test_a_connect_to_a_private_destination_is_refused(swap: ModuleType) -> None:
    addon = _swap(swap, FakeExchange(swap, set(), {}), "127.0.0.1")
    flow = _flow("https", "exchange.internal", {})

    await addon.http_connect(flow)

    assert flow.response.status_code == 403


async def test_a_connect_to_a_public_destination_passes(swap: ModuleType) -> None:
    addon = _swap(swap, FakeExchange(swap, set(), {}), "104.18.0.1")
    flow = _flow("https", "api.anthropic.com", {})

    await addon.http_connect(flow)

    assert flow.response is None


async def test_every_server_connection_is_checked_again_before_it_is_made(
    swap: ModuleType,
) -> None:
    addon = _swap(swap, FakeExchange(swap, set(), {}), "192.168.1.1")
    data = _server("rebound.test")

    await addon.server_connect(data)

    assert data.server.error == "destination refused"


async def test_a_server_connection_is_pinned_to_the_address_checked_and_keeps_the_name(
    swap: ModuleType,
) -> None:
    addon = _swap(swap, FakeExchange(swap, set(), {}), "2606:4700::1", "104.18.0.1", "104.18.0.2")
    data = _server("api.anthropic.com")

    await addon.server_connect(data)

    assert data.server.error is None
    assert data.server.address == ("104.18.0.1", 443)
    assert data.server.sni == "api.anthropic.com"


async def test_an_address_literal_gets_no_sni(swap: ModuleType) -> None:
    addon = _swap(swap, FakeExchange(swap, set(), {}), "104.18.0.1")
    data = _server("104.18.0.1", 8443)

    await addon.server_connect(data)

    assert data.server.address == ("104.18.0.1", 8443)
    assert data.server.sni is None


async def test_no_connection_is_made_before_the_exchange_has_answered_once(
    swap: ModuleType,
) -> None:
    exchange = FakeExchange(swap, {"api.anthropic.com"}, {})
    exchange.unavailable = True
    addon = _swap(swap, exchange, "104.18.0.1")
    data = _server("example.com")

    await addon.server_connect(data)
    assert data.server.error == "the exchange has not answered yet"

    exchange.unavailable = False
    addon._upstreams_at = float("-inf")
    later = _server("example.com")
    await addon.server_connect(later)
    assert later.server.error is None


async def test_tls_is_terminated_only_for_an_upstream_and_the_list_is_kept_for_a_while(
    swap: ModuleType,
) -> None:
    exchange = FakeExchange(swap, {"api.anthropic.com"}, {})
    addon = _swap(swap, exchange, "104.18.0.1")

    anthropic = SimpleNamespace(
        context=SimpleNamespace(
            server=SimpleNamespace(address=("api.anthropic.com", 443), sni=None)
        ),
        ignore_connection=False,
    )
    other = SimpleNamespace(
        context=SimpleNamespace(server=SimpleNamespace(address=("example.com", 443), sni=None)),
        ignore_connection=False,
    )
    await addon.tls_clienthello(anthropic)
    await addon.tls_clienthello(other)

    assert anthropic.ignore_connection is False
    assert anthropic.context.server.sni == "api.anthropic.com"
    assert other.ignore_connection is True
    assert exchange.upstreams_calls == 1


async def test_the_connect_host_decides_and_the_client_sni_does_not(swap: ModuleType) -> None:
    # A pinned connection carries an address. The CONNECT host lives in the SNI.
    exchange = FakeExchange(swap, {"api.anthropic.com"}, {})
    addon = _swap(swap, exchange, "104.18.0.1")
    pinned = SimpleNamespace(
        context=SimpleNamespace(
            server=SimpleNamespace(address=("104.18.0.1", 443), sni="api.anthropic.com")
        ),
        client_hello=SimpleNamespace(sni="evil.example"),
        ignore_connection=False,
    )

    await addon.tls_clienthello(pinned)

    assert pinned.ignore_connection is False
    assert pinned.context.server.sni == "api.anthropic.com"


async def test_the_last_upstreams_stand_while_the_exchange_is_down(swap: ModuleType) -> None:
    exchange = FakeExchange(swap, {"api.anthropic.com"}, {})
    addon = _swap(swap, exchange, "104.18.0.1")
    assert await addon.upstreams() == {"api.anthropic.com"}
    addon._upstreams_at = float("-inf")
    exchange.unavailable = True

    assert await addon.upstreams() == {"api.anthropic.com"}


async def test_the_placeholder_is_swapped_for_the_credential_the_exchange_names(
    swap: ModuleType,
) -> None:
    exchange = FakeExchange(
        swap,
        {"api.anthropic.com"},
        {(PLACEHOLDER, "api.anthropic.com"): ("Authorization", "Bearer sk-ant-real")},
    )
    addon = _swap(swap, exchange, "104.18.0.1")
    flow = _flow(
        "https", "api.anthropic.com", {"x-api-key": PLACEHOLDER, "anthropic-beta": "oauth"}
    )

    await addon.requestheaders(flow)

    assert flow.request.headers == {
        "Authorization": "Bearer sk-ant-real",
        "anthropic-beta": "oauth",
    }
    assert flow.request.stream is True
    assert flow.response is None
    assert exchange.asked == [(PLACEHOLDER, "api.anthropic.com")]


async def test_a_basic_placeholder_is_swapped_the_same_way(swap: ModuleType) -> None:
    basic = "Basic " + base64.b64encode(b"x-access-token:ghs_real").decode()
    exchange = FakeExchange(
        swap, {"github.com"}, {(PLACEHOLDER, "github.com"): ("Authorization", basic)}
    )
    addon = _swap(swap, exchange, "140.82.121.4")
    flow = _flow("https", "github.com", {"Authorization": _basic("x-access-token", PLACEHOLDER)})

    await addon.requestheaders(flow)

    assert flow.request.headers == {"Authorization": basic}


async def test_a_host_header_that_differs_from_the_connect_host_is_refused(
    swap: ModuleType,
) -> None:
    exchange = FakeExchange(
        swap,
        {"api.anthropic.com", "evil.example"},
        {(PLACEHOLDER, "api.anthropic.com"): ("Authorization", "Bearer sk-ant-real")},
    )
    addon = _swap(swap, exchange, "104.18.0.1")
    flow = _flow(
        "https",
        "evil.example",
        {"Authorization": f"Bearer {PLACEHOLDER}"},
        connect_host="api.anthropic.com",
    )

    await addon.requestheaders(flow)

    assert flow.response.status_code == 403
    assert exchange.asked == []
    assert flow.request.headers == {"Authorization": f"Bearer {PLACEHOLDER}"}


async def test_a_request_without_a_placeholder_passes_untouched(swap: ModuleType) -> None:
    exchange = FakeExchange(swap, {"api.anthropic.com"}, {})
    addon = _swap(swap, exchange, "104.18.0.1")
    flow = _flow("https", "api.anthropic.com", {"Authorization": "Bearer sk-ant-own"})

    await addon.requestheaders(flow)

    assert flow.request.headers == {"Authorization": "Bearer sk-ant-own"}
    assert flow.request.stream is True
    assert exchange.asked == []


async def test_a_refused_placeholder_answers_403_and_never_reaches_the_upstream(
    swap: ModuleType,
) -> None:
    exchange = FakeExchange(swap, {"api.anthropic.com"}, {})
    addon = _swap(swap, exchange, "104.18.0.1")
    flow = _flow("https", "api.anthropic.com", {"Authorization": f"Bearer {PLACEHOLDER}"})

    await addon.requestheaders(flow)

    assert flow.response.status_code == 403
    assert flow.request.headers == {"Authorization": f"Bearer {PLACEHOLDER}"}
    assert flow.request.stream is False


async def test_an_exchange_without_an_answer_gives_503(swap: ModuleType) -> None:
    exchange = FakeExchange(swap, {"api.anthropic.com"}, {})
    exchange.unavailable = True
    addon = _swap(swap, exchange, "104.18.0.1")
    flow = _flow("https", "api.anthropic.com", {"Authorization": f"Bearer {PLACEHOLDER}"})

    await addon.requestheaders(flow)

    assert flow.response.status_code == 503


async def test_plain_http_is_never_swapped_and_private_destinations_are_refused(
    swap: ModuleType,
) -> None:
    exchange = FakeExchange(swap, {"api.anthropic.com"}, {})
    addon = _swap(swap, exchange, "10.0.0.5")
    refused = _flow("http", "exchange.internal", {"Authorization": f"Bearer {PLACEHOLDER}"})
    await addon.requestheaders(refused)
    assert refused.response.status_code == 403

    addon.resolve = AsyncMock(return_value={ipaddress.ip_address("93.184.216.34")})
    passed = _flow("http", "example.com", {"Authorization": f"Bearer {PLACEHOLDER}"})
    await addon.requestheaders(passed)
    assert passed.response is None
    assert passed.request.headers == {"Authorization": f"Bearer {PLACEHOLDER}"}
    assert passed.request.stream is True
    assert exchange.asked == []


def test_responses_stream(swap: ModuleType) -> None:
    flow = SimpleNamespace(response=SimpleNamespace(stream=False))

    swap.Swap().responseheaders(flow)

    assert flow.response.stream is True


async def test_the_exchange_client_reads_the_answers(swap: ModuleType) -> None:
    exchange = swap.Exchange("http://exchange.test/")
    fetched: list[Any] = []

    async def fetch(request: Any, raise_error: bool) -> Any:
        fetched.append(request)
        if request.url.endswith("/upstreams"):
            return SimpleNamespace(code=200, headers={}, body=b'["api.anthropic.com"]')
        if request.headers["Authorization"] == f"Bearer {PLACEHOLDER}":
            return SimpleNamespace(
                code=200,
                headers={"X-Upstream-Header": "Authorization", "X-Upstream-Credential": "Bearer r"},
                body=b"",
            )
        return SimpleNamespace(code=403, headers={}, body=b"")

    exchange.client = SimpleNamespace(fetch=fetch)

    assert await exchange.upstreams() == {"api.anthropic.com"}
    assert await exchange.authorize(PLACEHOLDER, "api.anthropic.com") == (
        "Authorization",
        "Bearer r",
    )
    with pytest.raises(swap.Refused):
        await exchange.authorize("drk.0123abcd.anthropic.wrong", "api.anthropic.com")
    assert fetched[1].url == "http://exchange.test/authorize"
    assert fetched[1].headers["X-Forwarded-Host"] == "api.anthropic.com"


async def test_a_timeout_at_the_exchange_is_unavailable_not_an_escape(swap: ModuleType) -> None:
    exchange = swap.Exchange("http://exchange.test")
    error = sys.modules["tornado.httpclient"].HTTPClientError  # type: ignore[attr-defined]

    async def fetch(request: Any, raise_error: bool) -> Any:
        raise error("timeout")

    exchange.client = SimpleNamespace(fetch=fetch)

    with pytest.raises(swap.ExchangeUnavailable):
        await exchange.authorize(PLACEHOLDER, "api.anthropic.com")
