import asyncio
import hashlib
import socket
import ssl
import stat
import uuid
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncssh
import httpx
import pytest
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from secret_proxy import TUNNEL_IDENTITY_PREFIX
from secret_proxy.certificates import CertificateAuthority
from secret_proxy.client import SecretProxyClient
from secret_proxy.server import PlaceholderSubstitution, SecretProxyServer
from secret_proxy.settings import SecretProxySettings
from secret_proxy.tunnels import ReverseTunnel


class NoneAuthForwardingSSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        return listen_host == "127.0.0.1"


def _socket_path() -> Path:
    return Path("/tmp") / f"drukbox-test-{uuid4().hex}.sock"


def test_placeholder_substitution_handles_chunk_boundaries() -> None:
    substitution = PlaceholderSubstitution([{"placeholder": "placeholder", "value": "secret"}])

    output = substitution.replace(b"before-place")
    output += substitution.replace(b"holder-after")
    output += substitution.replace(b"", final=True)

    assert output == b"before-secret-after"


def test_header_substitution_does_not_reprocess_injected_values() -> None:
    headers = SecretProxyServer._upstream_headers(
        [
            (b"Authorization", b"Bearer client-secret"),
            (b"Authorization", b"Bearer short-placeholder"),
        ],
        [
            {"placeholder": "short-placeholder", "value": "long-placeholder"},
            {"placeholder": "long-placeholder", "value": "wrong-secret"},
        ],
    )

    assert headers == [("Authorization", "Bearer long-placeholder")]


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


def test_certificate_authority_rejects_an_exposed_ca_key(tmp_path) -> None:
    authority = CertificateAuthority(tmp_path)
    authority.ca_key_path.chmod(0o644)

    with pytest.raises(ValueError, match="group or world accessible"):
        CertificateAuthority(tmp_path)


@pytest.mark.asyncio
async def test_bare_host_tunnel_injects_headers_and_bodies(tmp_path) -> None:
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
        name="openai",
        host=host,
        placeholder="placeholder",
        value="real-secret",
    )
    _, proxy_port = proxy.address
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        box_port = int(reservation.getsockname()[1])
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    ssh_server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=NoneAuthForwardingSSHServer,
        server_host_keys=[host_key],
    )
    ssh_port = ssh_server.get_port()
    tunnel = await ReverseTunnel.open(
        host_id=uuid.uuid4(),
        host_name="box-one",
        ssh_host="127.0.0.1",
        ssh_port=ssh_port,
        ssh_username="root",
        known_hosts=(
            f"[127.0.0.1]:{ssh_port} {host_key.export_public_key('openssh').decode().strip()}\n"
        ),
        client_key=None,
        settings=settings.model_copy(update={"bind_port": proxy_port, "tunnel_box_port": box_port}),
        dropped=AsyncMock(),
    )
    proxy_url = f"http://127.0.0.1:{box_port}"
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
                    "Authorization": "Bearer placeholder",
                    "Cookie": "session=client-value",
                    "Expect": "100-continue",
                    "X-Forwarded-For": "198.51.100.1",
                },
            )
    finally:
        await tunnel.aclose()
        ssh_server.close()
        await ssh_server.wait_closed()
        await proxy.close()
        await runner.cleanup()

    assert response.status_code == 200
    assert response.content == b"upstream-response"
    assert received["method"] == "POST"
    assert received["path"] == "/request?mode=proxy"
    assert received["body"] == b"token=real-secret"
    received_headers = cast(dict[str, str], received["headers"])
    assert received_headers["Authorization"] == "Bearer real-secret"
    assert "Cookie" not in received_headers
    assert "Expect" not in received_headers
    assert "X-Forwarded-For" not in received_headers
    assert "Proxy-Authorization" not in received_headers


@pytest.mark.asyncio
async def test_shared_sbx_path_never_serves_a_registered_value(tmp_path) -> None:
    received_authorization = ""

    async def upstream(request: web.Request) -> web.Response:
        nonlocal received_authorization
        received_authorization = request.headers["Authorization"]
        return web.Response(text="blind-response")

    application = web.Application()
    application.router.add_get("/", upstream)
    runner = web.AppRunner(application)
    await runner.setup()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
        name="openai",
        host=host,
        placeholder="placeholder",
        value="real-secret",
    )
    proxy_host, proxy_port = proxy.address
    trust = ssl.create_default_context(cafile=proxy.certificates.ca_certificate_path)
    try:
        async with httpx.AsyncClient(
            proxy=f"http://box-one:spoofed@{proxy_host}:{proxy_port}",
            verify=trust,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"https://{host}/",
                headers={"Authorization": "Bearer placeholder"},
            )
    finally:
        await proxy.close()
        await runner.cleanup()

    assert response.text == "blind-response"
    assert received_authorization == "Bearer placeholder"


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
    runner, proxy, forwarder, client, host = await _start_proxy_fixture(
        tmp_path,
        application,
    )
    try:
        response = await client.get(f"https://{host}/redirect")
    finally:
        await client.aclose()
        forwarder.close()
        await forwarder.wait_closed()
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
    runner, proxy, forwarder, client, host = await _start_proxy_fixture(
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
        forwarder.close()
        await forwarder.wait_closed()
        await proxy.close()
        await runner.cleanup()

    assert first == b"first-chunk"
    assert rest == b"second-chunk"


@pytest.mark.asyncio
async def test_httpx_uses_the_installed_proxy_environment(tmp_path, monkeypatch) -> None:
    async def upstream(request: web.Request) -> web.Response:
        return web.Response(text="trusted")

    application = web.Application()
    application.router.add_get("/", upstream)
    runner, proxy, forwarder, configured_client, host = await _start_proxy_fixture(
        tmp_path,
        application,
    )
    await configured_client.aclose()
    proxy_port = int(forwarder.sockets[0].getsockname()[1])
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("SSL_CERT_FILE", str(proxy.certificates.ca_certificate_path))
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"https://{host}/")
    finally:
        forwarder.close()
        await forwarder.wait_closed()
        await proxy.close()
        await runner.cleanup()

    assert response.text == "trusted"


@pytest.mark.asyncio
async def test_proxy_blind_tunnels_an_unregistered_host_for_an_authorized_box(tmp_path) -> None:
    reached = False

    async def upstream(request: web.Request) -> web.Response:
        nonlocal reached
        reached = True
        return web.Response(text="untouched")

    application = web.Application()
    application.router.add_get("/", upstream)
    runner, proxy, forwarder, client, registered_host = await _start_proxy_fixture(
        tmp_path,
        application,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    site = web.SockSite(
        runner,
        listener,
        ssl_context=proxy.certificates.server_context("127.0.0.1"),
    )
    await site.start()
    other_host = f"127.0.0.1:{listener.getsockname()[1]}"
    assert other_host != registered_host
    try:
        response = await client.get(f"https://{other_host}/")
    finally:
        await client.aclose()
        forwarder.close()
        await forwarder.wait_closed()
        await proxy.close()
        await runner.cleanup()

    assert response.text == "untouched"
    assert reached


async def _start_proxy_fixture(
    tmp_path,
    application: web.Application,
) -> tuple[web.AppRunner, SecretProxyServer, asyncio.Server, httpx.AsyncClient, str]:
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
        name="openai",
        host=host,
        placeholder="placeholder",
        value="real-secret",
    )
    proxy_host, proxy_port = proxy.address
    forwarder = await asyncio.start_server(
        partial(_forward_with_identity, target_host=proxy_host, target_port=proxy_port),
        "127.0.0.1",
        0,
    )
    proxy_url = f"http://127.0.0.1:{int(forwarder.sockets[0].getsockname()[1])}"
    trust = ssl.create_default_context(cafile=proxy.certificates.ca_certificate_path)
    client = httpx.AsyncClient(proxy=proxy_url, verify=trust, trust_env=False)
    return runner, proxy, forwarder, client, host


async def _forward_with_identity(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    target_host: str,
    target_port: int,
) -> None:
    target_reader, target_writer = await asyncio.open_connection(target_host, target_port)
    target_writer.write(TUNNEL_IDENTITY_PREFIX + b"box-one\r\n")
    await target_writer.drain()
    try:
        await asyncio.gather(
            _copy_stream(reader, target_writer),
            _copy_stream(target_reader, writer),
        )
    finally:
        target_writer.close()
        writer.close()
        await asyncio.gather(
            target_writer.wait_closed(),
            writer.wait_closed(),
            return_exceptions=True,
        )


async def _copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()
