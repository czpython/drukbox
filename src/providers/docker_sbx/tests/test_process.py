import asyncio
import os

import pytest

from providers.base import TerminalSize
from providers.docker_sbx.process import SbxExecProcess
from providers.exceptions import ProviderTransportError

_REAL_EXEC = asyncio.create_subprocess_exec


def _stub_sbx_with(script: str, captured: dict):
    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        return await _REAL_EXEC("bash", "-c", script, **kwargs)

    return fake_exec


async def _drain(session: SbxExecProcess) -> bytes:
    output = bytearray()
    while data := await session.receive(4096):
        output.extend(data)
    return bytes(output)


async def test_exec_session_bridges_pipes_in_both_directions(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "providers.docker_sbx.process.asyncio.create_subprocess_exec",
        _stub_sbx_with("cat; exit 5", captured),
    )

    session = await SbxExecProcess.open("sb-test", command="true", terminal=None)
    session.send(b"round-trip")
    session.send_eof()
    output = await _drain(session)
    status = await session.wait()
    await session.aclose()

    assert captured["argv"] == (
        "sbx",
        "exec",
        "--interactive",
        "sb-test",
        "bash",
        "-l",
        "-c",
        "true",
    )
    assert output == b"round-trip"
    assert status == 5


async def test_exec_session_keeps_the_error_stream_separate(monkeypatch):
    # The sbx wake banner goes to stderr; it must not arrive in the output.
    monkeypatch.setattr(
        "providers.docker_sbx.process.asyncio.create_subprocess_exec",
        _stub_sbx_with("echo OUT; echo BANNER >&2; exit 0", {}),
    )

    session = await SbxExecProcess.open("sb-test", command="true", terminal=None)
    output = await _drain(session)
    stderr = await session.receive_stderr(4096)
    await session.aclose()

    assert output == b"OUT\n"
    assert stderr == b"BANNER\n"


async def test_terminal_request_allocates_a_real_pty_at_the_requested_size(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "providers.docker_sbx.process.asyncio.create_subprocess_exec",
        _stub_sbx_with("test -t 0 && echo ISTTY; stty size; exit 3", captured),
    )

    session = await SbxExecProcess.open(
        "sb-test", command=None, terminal=TerminalSize(columns=101, rows=42)
    )
    output = await _drain(session)
    status = await session.wait()
    await session.aclose()

    assert "--tty" in captured["argv"]
    assert captured["argv"][-2:] == ("bash", "-l")
    assert b"ISTTY" in output
    assert b"42 101" in output
    assert status == 3


async def test_aclose_releases_the_terminal_descriptor(monkeypatch):
    monkeypatch.setattr(
        "providers.docker_sbx.process.asyncio.create_subprocess_exec",
        _stub_sbx_with("exit 0", {}),
    )

    session = await SbxExecProcess.open(
        "sb-test", command=None, terminal=TerminalSize(columns=80, rows=24)
    )
    master = session._pty_master
    assert master is not None
    await session.aclose()

    with pytest.raises(OSError):
        os.fstat(master)


async def test_missing_sbx_binary_maps_to_transport_error(monkeypatch):
    async def fail_exec(*argv, **kwargs):
        raise FileNotFoundError("sbx")

    monkeypatch.setattr("providers.docker_sbx.process.asyncio.create_subprocess_exec", fail_exec)

    with pytest.raises(ProviderTransportError, match="could not be started"):
        await SbxExecProcess.open("sb-test", command=None, terminal=None)
