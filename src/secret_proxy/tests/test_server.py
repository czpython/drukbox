import asyncio
import hashlib
import socket
import ssl
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from secret_proxy.certificates import CertificateAuthority
from secret_proxy.client import SecretProxyClient
from secret_proxy.server import PlaceholderSubstitution, SecretProxyServer
from secret_proxy.settings import SecretProxySettings


def _socket_path() -> Path:
    return Path("/tmp") / f"drukbox-test-{uuid4().hex}.sock"


def test_placeholder_substitution_handles_chunk_boundaries() -> None:
    substitution = PlaceholderSubstitution([{"placeholder": "placeholder", "value": "secret"}])

    output = substitution.replace(b"before-place")
    output += substitution.replace(b"holder-after")
    output += substitution.replace(b"", final=True)

    assert output == b"before-secret-after"


def test_certificate_authority_replaces_a_mismatched_cached_leaf(tmp_path) -> None:
    authority = CertificateAuthority(tmp_path)
    authority.server_context("api.example.com")
    stem = hashlib.sha256(b"api.example.com").hexdigest()
    key_path = tmp_path / f"{stem}.key"
    wrong_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path.write_bytes(wrong_key)

    authority.server_context("api.example.com")

    assert key_path.read_bytes() != wrong_key


@pytest.mark.asyncio
async def test_proxy_injects_headers_and_bodies_without_forwarding_client_auth(tmp_path) -> None:
    received: dict[str, object] = {}

    async def upstream(request: web.Request) -> web.Response:
        received.update(
            method=request.method,
            path=request.path_qs,
            headers=dict(request.headers),
            body=await request.read(),
        )
        return web.Response(body=b"upstream-response")

    application = web.Application()
    application.router.add_route("*", "/request", upstream)
    runner = web.AppRunner(application)
    await runner.setup()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)

    settings = SecretProxySettings(
        bind_port=0,
        control_socket=_socket_path(),
        certificate_directory=tmp_path / "certificates",
        allow_private_upstreams=True,
    )
    proxy = SecretProxyServer(settings, upstream_ssl=False)
    site = web.SockSite(
        runner,
        listener,
        ssl_context=proxy.certificates.server_context("127.0.0.1"),
    )
    await site.start()
    upstream_port = listener.getsockname()[1]
    await proxy.start()
    assert stat.S_IMODE(proxy.certificates.ca_key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.expanded_control_socket.stat().st_mode) == 0o600
    control = SecretProxyClient(settings.expanded_control_socket)
    host = f"127.0.0.1:{upstream_port}"
    await control.put_secret(
        vm="box-one",
        host=host,
        env_var="API_TOKEN",
        headers={"Authorization": "Bearer placeholder"},
        placeholder="placeholder",
        value="real-secret",
    )
    route = await control.route(vm="box-one")
    proxy_host, proxy_port = proxy.address
    proxy_url = f"http://{route['username']}:{route['password']}@{proxy_host}:{proxy_port}"
    trust = ssl.create_default_context(cafile=proxy.certificates.ca_certificate_path)

    async def body() -> AsyncIterator[bytes]:
        yield b"token=place"
        yield b"holder"

    try:
        async with httpx.AsyncClient(proxy=proxy_url, verify=trust, trust_env=False) as client:
            response = await client.post(
                f"https://{host}/request?mode=proxy",
                content=body(),
                headers={
                    "Authorization": "Bearer client-value",
                    "Cookie": "session=client-value",
                    "Expect": "100-continue",
                    "X-Forwarded-For": "198.51.100.1",
                },
            )
    finally:
        await proxy.close()
        await runner.cleanup()

    assert response.status_code == 200
    assert response.content == b"upstream-response"
    assert received["method"] == "POST"
    assert received["path"] == "/request?mode=proxy"
    assert received["body"] == b"token=real-secret"
    received_headers = received["headers"]
    assert isinstance(received_headers, dict)
    assert received_headers["Authorization"] == "Bearer real-secret"
    assert "Cookie" not in received_headers
    assert "Expect" not in received_headers
    assert "X-Forwarded-For" not in received_headers
    assert "Proxy-Authorization" not in received_headers


@pytest.mark.asyncio
async def test_proxy_returns_redirects_without_following_them(tmp_path) -> None:
    followed = False

    async def redirect(request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": "/destination"})

    async def destination(request: web.Request) -> web.Response:
        nonlocal followed
        followed = True
        return web.Response(text="not permitted")

    application = web.Application()
    application.router.add_get("/redirect", redirect)
    application.router.add_get("/destination", destination)
    runner, proxy, client, host = await _start_proxy_fixture(
        tmp_path,
        application,
    )
    try:
        response = await client.get(f"https://{host}/redirect")
    finally:
        await client.aclose()
        await proxy.close()
        await runner.cleanup()

    assert response.status_code == 302
    assert not followed


@pytest.mark.asyncio
async def test_proxy_streams_the_upstream_response(tmp_path) -> None:
    release = asyncio.Event()

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"first-chunk")
        await release.wait()
        await response.write(b"second-chunk")
        return response

    application = web.Application()
    application.router.add_get("/stream", stream)
    runner, proxy, client, host = await _start_proxy_fixture(
        tmp_path,
        application,
    )
    try:
        async with client.stream("GET", f"https://{host}/stream") as response:
            chunks = response.aiter_raw()
            first = await asyncio.wait_for(anext(chunks), timeout=2)
            release.set()
            rest = b"".join([chunk async for chunk in chunks])
    finally:
        release.set()
        await client.aclose()
        await proxy.close()
        await runner.cleanup()

    assert first == b"first-chunk"
    assert rest == b"second-chunk"


@pytest.mark.asyncio
async def test_proxy_rejects_an_unregistered_host_for_an_authorized_box(tmp_path) -> None:
    async def upstream(request: web.Request) -> web.Response:
        return web.Response(text="not reached")

    application = web.Application()
    application.router.add_get("/", upstream)
    runner, proxy, client, _ = await _start_proxy_fixture(
        tmp_path,
        application,
    )
    try:
        with pytest.raises(httpx.ProxyError):
            await client.get("https://example.com/")
    finally:
        await client.aclose()
        await proxy.close()
        await runner.cleanup()


async def _start_proxy_fixture(
    tmp_path,
    application: web.Application,
) -> tuple[web.AppRunner, SecretProxyServer, httpx.AsyncClient, str]:
    runner = web.AppRunner(application)
    await runner.setup()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    settings = SecretProxySettings(
        bind_port=0,
        control_socket=_socket_path(),
        certificate_directory=tmp_path / "certificates",
        allow_private_upstreams=True,
    )
    proxy = SecretProxyServer(settings, upstream_ssl=False)
    site = web.SockSite(
        runner,
        listener,
        ssl_context=proxy.certificates.server_context("127.0.0.1"),
    )
    await site.start()
    host = f"127.0.0.1:{listener.getsockname()[1]}"
    await proxy.start()
    control = SecretProxyClient(settings.expanded_control_socket)
    await control.put_secret(
        vm="box-one",
        host=host,
        env_var="API_TOKEN",
        headers={"Authorization": "Bearer placeholder"},
        placeholder="placeholder",
        value="real-secret",
    )
    route = await control.route(vm="box-one")
    proxy_host, proxy_port = proxy.address
    proxy_url = f"http://{route['username']}:{route['password']}@{proxy_host}:{proxy_port}"
    trust = ssl.create_default_context(cafile=proxy.certificates.ca_certificate_path)
    client = httpx.AsyncClient(proxy=proxy_url, verify=trust, trust_env=False)
    return runner, proxy, client, host
