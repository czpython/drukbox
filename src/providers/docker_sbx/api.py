import asyncio
import json
import re

from .exceptions import DockerSbxNotFoundError, DockerSbxTransportError

# The first `sbx create` can pull a large template. The time limit is large
# because it must stop only a blocked daemon, not a slow pull.
_SBX_TIMEOUT_SECONDS = 600.0

# Only the CLI message for a missing sandbox is a not-found error. Messages
# such as "credentials not found" must stay transport errors. If not,
# delete_vm can identify a live sandbox as removed.
_SANDBOX_NOT_FOUND_RE = re.compile(r"sandbox '[^']*' not found")


class SbxCLI:
    """Thin async wrapper for the local ``sbx`` command-line interface.

    Each method starts ``sbx`` with ``create_subprocess_exec``. Thus the
    subprocess boundary stays in one place. The CLI selects the daemon: the
    user socket by default, or the socket that ``DOCKER_SANDBOXES_API`` gives
    when drukbox runs in a container.
    """

    async def create_sandbox(
        self,
        *,
        name: str,
        template: str,
        workspace: str,
        cpus: int,
        memory: str,
    ) -> None:
        # The `shell` agent makes the sandbox start the template entrypoint,
        # not an AI agent. The sizes are always explicit. Without them, the
        # daemon gives one sandbox all host CPUs and half of the host memory.
        await self._run(
            "create",
            "--name",
            name,
            "--template",
            template,
            "--cpus",
            str(cpus),
            "--memory",
            memory,
            "--quiet",
            "shell",
            workspace,
        )

    async def run_bootstrap(self, name: str, script: str) -> None:
        # The script contains caller environment values. All processes can
        # read argv through /proc. Thus the script goes through stdin, not
        # argv. `bash -s` reads the program from stdin.
        await self._run(
            "exec",
            "--interactive",
            "--user",
            "root",
            name,
            "bash",
            "-s",
            stdin=script,
        )

    async def remove_sandbox(self, name: str) -> None:
        # The --force flag stops the confirmation prompt. It also removes a
        # sandbox that has an open SSH session.
        await self._run("rm", "--force", name)

    async def set_secret(self, service: str, *, sandbox: str, command: str) -> None:
        # --token puts the value in argv, which every process can read. The
        # command runs on the host, under sandboxd, and its output is the value.
        await self._run("secret", "set", service, "--sandbox", sandbox, "--command", command)

    async def set_custom_secret(
        self,
        *,
        sandbox: str,
        host: str,
        env: str,
        placeholder: str,
        command: str,
    ) -> None:
        await self._run(
            "secret",
            "set-custom",
            "--sandbox",
            sandbox,
            "--host",
            host,
            "--env",
            env,
            "--placeholder",
            placeholder,
            "--command",
            command,
        )

    async def remove_secret(self, service: str, *, sandbox: str) -> None:
        # Without -f the CLI asks for confirmation and waits forever.
        await self._run("secret", "rm", "-f", service, "--sandbox", sandbox)

    async def remove_custom_secret(self, *, sandbox: str, placeholder: str) -> None:
        await self._run("secret", "rm", "-f", "--placeholder", placeholder, "--sandbox", sandbox)

    async def sandbox_count(self) -> int:
        output = await self._run("ls", "--json")
        try:
            payload = json.loads(output)
            # Go writes an empty list as null. An unused daemon shows null.
            return len(payload["sandboxes"] or [])
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise DockerSbxTransportError(
                f"sbx ls returned an unreadable sandbox list: {output.strip()!r}"
            ) from error

    async def _run(self, *args: str, stdin: str | None = None) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "sbx",
                *args,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            # The binary can be missing (FileNotFoundError) or not executable
            # (PermissionError). Translate each OSError type. A raw OSError
            # must not go out of the provider boundary.
            raise DockerSbxTransportError(f"sbx CLI could not be started: {error}") from error
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin else None),
                timeout=_SBX_TIMEOUT_SECONDS,
            )
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise DockerSbxTransportError(
                f"sbx {args[0]} did not finish within {_SBX_TIMEOUT_SECONDS:.0f}s"
            ) from error
        if process.returncode != 0:
            detail = stderr.decode().strip() or f"sbx {args[0]} exited {process.returncode}"
            if _SANDBOX_NOT_FOUND_RE.search(detail):
                raise DockerSbxNotFoundError(detail)
            raise DockerSbxTransportError(detail)
        return stdout.decode()
