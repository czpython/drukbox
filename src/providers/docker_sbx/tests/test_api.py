import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from providers.docker_sbx.api import SbxCLI
from providers.docker_sbx.exceptions import (
    DockerSbxNotFoundError,
    DockerSbxTransportError,
)


def _process(*, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(
        returncode=returncode,
        communicate=AsyncMock(return_value=(stdout, stderr)),
    )


@pytest.mark.asyncio
async def test_create_sandbox_requests_the_shell_agent_with_explicit_sizing(monkeypatch):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _process()

    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", fake_exec)

    await SbxCLI().create_sandbox(
        name="sb-test",
        template="drukbox/sbx-sandbox:latest",
        workspace="/var/lib/drukbox/sbx-workspaces/sb-test",
        cpus=4,
        memory="8g",
    )

    args = captured["args"]
    # The `shell` agent makes the sandbox start the template entrypoint.
    # The workspace is the last positional argument.
    assert args[-2:] == ("shell", "/var/lib/drukbox/sbx-workspaces/sb-test")
    assert args[args.index("--template") + 1] == "drukbox/sbx-sandbox:latest"
    # Without the size flags, the daemon gives the sandbox all host CPUs
    # and half of the host memory.
    assert args[args.index("--cpus") + 1] == "4"
    assert args[args.index("--memory") + 1] == "8g"


@pytest.mark.asyncio
async def test_run_bootstrap_feeds_the_script_over_stdin_never_argv(monkeypatch):
    process = _process()
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["stdin"] = kwargs["stdin"]
        return process

    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", fake_exec)

    script = "printf '%s\\n' 'API_TOKEN=s3cr3t' >> /etc/environment\n"
    await SbxCLI().run_bootstrap("sb-test", script)

    # All processes can read argv through /proc. A caller secret must not
    # show in argv. The check examines each token for the substring.
    assert all("s3cr3t" not in arg for arg in captured["args"])
    assert captured["stdin"] == asyncio.subprocess.PIPE
    process.communicate.assert_awaited_once_with(script.encode())


@pytest.mark.asyncio
async def test_set_custom_secret_names_every_host_of_the_service(monkeypatch):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _process()

    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", fake_exec)

    await SbxCLI().set_custom_secret(
        sandbox="sb-test",
        hosts=["api.github.com", "github.com"],
        env="GH_TOKEN",
        placeholder="drk.a.github.b",
        command="cat /secrets/sb-test/github",
    )

    assert captured["args"] == (
        "sbx",
        "secret",
        "set-custom",
        "--sandbox",
        "sb-test",
        "--host",
        "api.github.com",
        "--host",
        "github.com",
        "--env",
        "GH_TOKEN",
        "--placeholder",
        "drk.a.github.b",
        "--command",
        "cat /secrets/sb-test/github",
    )


LISTING = """SCOPE  TYPE  NAME  SECRET
sb-test  service  github  (stored)

CUSTOM SECRETS
SCOPE  TARGETS  ENV  PLACEHOLDER  SECRET
sb-test  api.anthropic.com  ANTHROPIC_AUTH_TOKEN  drk.0123.anthropic.abc  cmd (on-demand)
sb-test  api.acme.test, acme.example  ACME_TOKEN  drk.0123.acme.def  cmd (on-demand)
"""


@pytest.mark.asyncio
async def test_custom_placeholders_reads_the_placeholder_of_each_custom_row(
    monkeypatch,
):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _process(stdout=LISTING.encode())

    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", fake_exec)

    placeholders = await SbxCLI().custom_placeholders(sandbox="sb-test")

    assert captured["args"] == ("sbx", "secret", "ls", "--sandbox", "sb-test")
    assert placeholders == ["drk.0123.anthropic.abc", "drk.0123.acme.def"]


@pytest.mark.asyncio
async def test_custom_placeholders_of_an_empty_scope(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _process(stdout=b'No secrets found for scope "sb-test".\n')

    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", fake_exec)

    assert await SbxCLI().custom_placeholders(sandbox="sb-test") == []


@pytest.mark.asyncio
async def test_sandbox_count_reads_the_listing(monkeypatch):
    listing = b'{"sandboxes": [{"name": "sb-a"}, {"name": "sb-b"}]}'
    create = AsyncMock(return_value=_process(stdout=listing))
    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", create)

    assert await SbxCLI().sandbox_count() == 2


@pytest.mark.asyncio
async def test_sandbox_count_treats_a_null_list_as_empty(monkeypatch):
    # Go writes an empty list as null. An unused daemon shows null.
    create = AsyncMock(return_value=_process(stdout=b'{"sandboxes": null}'))
    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", create)

    assert await SbxCLI().sandbox_count() == 0


@pytest.mark.asyncio
async def test_remove_sandbox_forces_removal_of_an_attached_sandbox(monkeypatch):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _process()

    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", fake_exec)

    await SbxCLI().remove_sandbox("sb-test")

    # Without --force, the CLI asks for confirmation. It also refuses a
    # sandbox that has an open SSH session.
    assert captured["args"] == ("sbx", "rm", "--force", "sb-test")


@pytest.mark.asyncio
async def test_missing_sandbox_maps_to_not_found(monkeypatch):
    create = AsyncMock(
        return_value=_process(
            returncode=1,
            stderr=b"Error: sandbox 'sb-test' not found (run 'sbx ls' to see your sandboxes)",
        )
    )
    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", create)

    with pytest.raises(DockerSbxNotFoundError):
        await SbxCLI().remove_sandbox("sb-test")


@pytest.mark.asyncio
async def test_stderr_merely_containing_not_found_stays_a_transport_error(monkeypatch):
    # Only the CLI message for a missing sandbox can map to not-found. If an
    # auth error maps to not-found, delete_vm removes the record and the
    # workspace of a live sandbox.
    create = AsyncMock(
        return_value=_process(returncode=1, stderr=b"Error: credentials not found; run 'sbx login'")
    )
    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", create)

    with pytest.raises(DockerSbxTransportError):
        await SbxCLI().remove_sandbox("sb-test")


@pytest.mark.asyncio
async def test_missing_sbx_binary_maps_to_transport_error(monkeypatch):
    create = AsyncMock(side_effect=FileNotFoundError("sbx"))
    monkeypatch.setattr("providers.docker_sbx.api.asyncio.create_subprocess_exec", create)

    with pytest.raises(DockerSbxTransportError, match="could not be started"):
        await SbxCLI().sandbox_count()
