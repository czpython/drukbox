import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from hosts.service import _SANDBOX_BOOTSTRAP_SCRIPT


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    with TemporaryDirectory(prefix="dbx-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.mark.parametrize("systemd", [False, True])
async def test_bootstrap_starts_tailscale_and_enables_ssh(tmp_path: Path, systemd: bool):
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "commands.log"
    socket_path = tmp_path / "var/run/tailscale/tailscaled.sock"
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
    (tmp_path / "var/log").mkdir(parents=True)
    if systemd:
        (tmp_path / "run/systemd/system").mkdir(parents=True)
    script = _SANDBOX_BOOTSTRAP_SCRIPT.replace("/var/", f"{tmp_path}/var/").replace(
        "[[ -d /run/systemd/system ]]", f"[[ -d {tmp_path}/run/systemd/system ]]"
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
    assert (tmp_path / "var/lib/sandbox/bootstrap.done").exists()
    if systemd:
        assert "systemctl enable --now tailscaled.service" in operations
        assert "--tun=userspace-networking" not in operations
    else:
        assert "--tun=userspace-networking --state=mem:" in operations
        assert "systemctl" not in operations
