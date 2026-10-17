import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from hosts.service import _SANDBOX_BOOTSTRAP_SCRIPT as SCRIPT


def test_bootstrap_script_fits_exe_setup_script_size_limit() -> None:
    # exe.dev's --setup-script is capped at 10 KiB. Encoded form (escape +
    # double-quote wrapping) is slightly larger; assert raw size first and
    # leave headroom for encoding overhead.
    assert len(SCRIPT.encode("utf-8")) < 9 * 1024


def test_bootstrap_script_has_bash_shebang() -> None:
    assert SCRIPT.splitlines()[0] == "#!/usr/bin/env bash"


def test_bootstrap_script_only_requires_tailscale_authkey() -> None:
    # Bootstrap's sole contract with the VM is TAILSCALE_AUTHKEY. Drukbox
    # detects the VM via the Tailscale API; the script does not call back.
    assert "TAILSCALE_AUTHKEY" in SCRIPT
    assert "REMOTE_HOST_ANNOUNCE_URL" not in SCRIPT
    assert "REMOTE_HOST_ANNOUNCE_TOKEN" not in SCRIPT


def test_bootstrap_script_does_not_call_drukbox() -> None:
    # No HTTP traffic from inside the box to the control plane.
    assert "curl" not in SCRIPT
    assert "Authorization" not in SCRIPT


def test_bootstrap_script_invokes_tailscale() -> None:
    assert 'run_privileged tailscale "${tailscale_args[@]}"' in SCRIPT
    assert "--authkey=$TAILSCALE_AUTHKEY" in SCRIPT
    assert "--ssh" in SCRIPT


def test_bootstrap_script_uses_privileged_service_operations() -> None:
    assert "sudo -n" in SCRIPT
    assert "run_privileged install -d" in SCRIPT
    assert "run_privileged systemctl enable --now tailscaled.service" in SCRIPT


@pytest.fixture
def short_path() -> Iterator[Path]:
    # A Unix socket path is capped near 108 bytes; pytest's short_path can exceed it.
    with TemporaryDirectory(prefix="dbx-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.mark.parametrize("systemd", [False, True])
async def test_bootstrap_starts_tailscale_and_enables_ssh(short_path: Path, systemd: bool):
    commands = short_path / "bin"
    commands.mkdir()
    log = short_path / "commands.log"
    socket_path = short_path / "var/run/tailscale/tailscaled.sock"
    scripts = {
        "sudo": '#!/bin/sh\nshift\nexec "$@"\n',
        "systemctl": f'#!/bin/sh\necho "systemctl $*" >> {log}\n',
        "tailscale": f'#!/bin/sh\necho "tailscale $*" >> {log}\n',
        "jq": "#!/bin/sh\ncat >/dev/null\nexit 1\n",
        "tailscaled": (
            f"#!{sys.executable}\nimport socket, sys\n"
            f"with open({str(log)!r}, 'a') as log:\n"
            "    log.write('tailscaled ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            f"connection = socket.socket(socket.AF_UNIX)\nconnection.bind({str(socket_path)!r})\n"
        ),
    }
    for name, contents in scripts.items():
        path = commands / name
        path.write_text(contents)
        path.chmod(0o755)
    (short_path / "var/log").mkdir(parents=True)
    if systemd:
        (short_path / "run/systemd/system").mkdir(parents=True)
    script = SCRIPT.replace("/var/", f"{short_path}/var/").replace(
        "[[ -d /run/systemd/system ]]", f"[[ -d {short_path}/run/systemd/system ]]"
    )
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        script,
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "TAILSCALE_AUTHKEY": "test-key",
            "TAILSCALE_HOSTNAME": "sb-test",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 0, (stdout, stderr)
    operations = log.read_text()
    assert "--ssh" in operations
    assert "--hostname=sb-test" in operations
    assert (short_path / "var/lib/sandbox/bootstrap.done").exists()
    if systemd:
        assert "systemctl enable --now tailscaled.service" in operations
        assert "--tun=userspace-networking" not in operations
    else:
        assert "--tun=userspace-networking --state=mem:" in operations
        assert "systemctl" not in operations
