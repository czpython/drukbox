import asyncio
import os
import shlex

import pytest

from providers.base import TerminalSize
from providers.docker_sbx.process import SbxExecProcess, _session_script
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

    assert captured["argv"][:7] == (
        "sbx",
        "exec",
        "--interactive",
        "sb-test",
        "bash",
        "-l",
        "-c",
    )
    assert captured["argv"][7] == _session_script("sb-test", "true", "root")
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
    assert captured["argv"][-4:-1] == ("bash", "-l", "-c")
    assert captured["argv"][-1] == _session_script("sb-test", None, "root")
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


def test_session_prepares_the_per_host_home_before_the_command():
    script = _session_script("sb-abc", "git status", "root")
    assert "mkdir -p /home/sb-abc" in script
    assert "export HOME=/home/sb-abc" in script
    # The home exists and HOME is set before the command runs.
    assert script.index("mkdir -p /home/sb-abc") < script.index("git status")
    assert script.index("export HOME=/home/sb-abc") < script.index("git status")


def test_session_without_a_command_runs_a_login_shell_in_the_home():
    script = _session_script("sb-abc", None, "root")
    assert "mkdir -p /home/sb-abc" in script
    assert shlex.quote("exec bash -l") in script


def test_root_session_uses_pam_without_changing_home_ownership():
    # PAM loads /etc/environment for the default root session too.
    assert _session_script("sb-abc", "git status", "root") == (
        "mkdir -p /home/sb-abc && cd /home/sb-abc && export HOME=/home/sb-abc\n"
        "exec su -m root -s /bin/bash -c 'git status'"
    )
    assert "chown" not in _session_script("sb-abc", None, "root")


def test_non_root_session_chowns_the_home_and_drops_to_the_user():
    script = _session_script("sb-abc", "agent run --flag", "druks")
    # Root prepares and gives the user the home before it drops.
    assert "chown druks /home/sb-abc" in script
    assert script.index("mkdir -p /home/sb-abc") < script.index("su -m druks")
    assert script.index("chown druks") < script.index("su -m druks")
    # su -m keeps the exported HOME; the payload is quoted for /bin/bash -c.
    assert f"exec su -m druks -s /bin/bash -c {shlex.quote('agent run --flag')}" in script


def test_non_root_interactive_session_drops_to_a_login_shell():
    script = _session_script("sb-abc", None, "druks")
    assert f"exec su -m druks -s /bin/bash -c {shlex.quote('exec bash -l')}" in script


def test_non_root_sftp_backing_shell_runs_as_the_user():
    # The SFTP backend passes its server command through the same open();
    # thus its backing shell drops to the user too.
    script = _session_script("sb-abc", "exec /usr/lib/openssh/sftp-server", "druks")
    assert "exec su -m druks -s /bin/bash -c" in script
    assert shlex.quote("exec /usr/lib/openssh/sftp-server") in script


async def test_open_runs_the_session_as_the_configured_user(monkeypatch):
    monkeypatch.setenv("DOCKER_SBX_SSH_USERNAME", "druks")
    captured: dict = {}
    monkeypatch.setattr(
        "providers.docker_sbx.process.asyncio.create_subprocess_exec",
        _stub_sbx_with("exit 0", captured),
    )

    session = await SbxExecProcess.open("sb-test", command="id -un", terminal=None)
    await session.wait()
    await session.aclose()

    assert captured["argv"][7] == _session_script("sb-test", "id -un", "druks")
    assert "exec su -m druks" in captured["argv"][7]


async def test_missing_sbx_binary_maps_to_transport_error(monkeypatch):
    async def fail_exec(*argv, **kwargs):
        raise FileNotFoundError("sbx")

    monkeypatch.setattr("providers.docker_sbx.process.asyncio.create_subprocess_exec", fail_exec)

    with pytest.raises(ProviderTransportError, match="could not be started"):
        await SbxExecProcess.open("sb-test", command=None, terminal=None)
