import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from secret_proxy.client import SecretProxyClient
from secret_proxy.control import SecretProxyControlServer
from secret_proxy.exceptions import SecretProxyRejectedError, SecretProxyUnavailableError
from secret_proxy.rules import SecretRules

_TEST_CA = """-----BEGIN CERTIFICATE-----
dGVzdA==
-----END CERTIFICATE-----
"""


def _socket_path() -> Path:
    return Path("/tmp") / f"drukbox-test-{uuid4().hex}.sock"


@pytest.mark.asyncio
async def test_client_controls_rules_through_the_private_socket(tmp_path) -> None:
    socket_path = _socket_path()
    rules = SecretRules(allow_private_upstreams=True)
    server = SecretProxyControlServer(socket_path, rules, ca_certificate=_TEST_CA)
    await server.start()
    client = SecretProxyClient(socket_path)
    try:
        await client.put_secret(
            vm="box-one",
            name="openai",
            host="127.0.0.1:8443",
            placeholder="placeholder",
            value="secret-value",
        )

        assert await client.list_secrets(vm="box-one") == ["openai"]
        assert await client.certificate_authority() == _TEST_CA

        await client.delete_secret(vm="box-one", name="openai")
        assert await client.list_secrets(vm="box-one") == []
    finally:
        await server.close()

    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_control_errors_do_not_return_request_values(tmp_path) -> None:
    socket_path = _socket_path()
    server = SecretProxyControlServer(
        socket_path,
        SecretRules(allow_private_upstreams=True),
        ca_certificate=_TEST_CA,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        secret = "must-not-return"
        writer.write(json.dumps({"operation": "unknown", "vm": secret}).encode() + b"\n")
        await writer.drain()
        response = await reader.readline()
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()

    assert response == b'{"ok":false,"error":"rejected"}\n'
    assert secret.encode() not in response


@pytest.mark.asyncio
async def test_client_raises_a_typed_error_for_a_missing_server(tmp_path) -> None:
    client = SecretProxyClient(tmp_path / "missing.sock")

    with pytest.raises(SecretProxyUnavailableError, match="unavailable"):
        await client.list_secrets(vm="box-one")


@pytest.mark.asyncio
async def test_client_raises_a_typed_error_for_a_rejected_request(tmp_path) -> None:
    socket_path = _socket_path()
    server = SecretProxyControlServer(
        socket_path,
        SecretRules(allow_private_upstreams=True),
        ca_certificate=_TEST_CA,
    )
    await server.start()
    client = SecretProxyClient(socket_path)
    try:
        with pytest.raises(SecretProxyRejectedError, match="rejected"):
            await client.delete_secret(vm="box-one", name="missing")
    finally:
        await server.close()
