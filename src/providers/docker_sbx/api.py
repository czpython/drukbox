import asyncio
import json
import re
from pathlib import Path

from .exceptions import DockerSbxNotFoundError, DockerSbxTransportError

# Long enough for a template pull. It stops a blocked daemon only.
_SBX_TIMEOUT_SECONDS = 600.0

# "credentials not found" must stay a transport error, or delete_vm takes a
# live sandbox for a removed one.
_NOT_FOUND_RE = re.compile(r"sandbox '[^']*' not found|no image \"[^\"]*\"")


class SbxCLI:
    """The local ``sbx`` command. ``DOCKER_SANDBOXES_API`` selects the daemon."""

    async def create_sandbox(
        self,
        *,
        name: str,
        template: str,
        workspace: str,
        cpus: int,
        memory: str,
    ) -> None:
        # The `shell` agent starts the template entrypoint. Without sizes the
        # daemon gives one sandbox all CPUs and half of the memory.
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
        # The script holds caller values, and every process can read argv.
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
        # --force also removes a sandbox with an open SSH session.
        await self._run("rm", "--force", name)

    async def set_secret(self, service: str, *, sandbox: str, command: str) -> None:
        # --token would put the value in argv. Without on-demand, sbx caches
        # the command's output for 55 minutes.
        await self._run(
            "secret",
            "set",
            service,
            "--sandbox",
            sandbox,
            "--command",
            command,
            "--refresh",
            "on-demand",
        )

    async def set_custom_secret(
        self,
        *,
        sandbox: str,
        hosts: list[str],
        env: str,
        placeholder: str,
        command: str,
    ) -> None:
        # --host repeats. sbx runs the command at each use by default.
        await self._run(
            "secret",
            "set-custom",
            "--sandbox",
            sandbox,
            *(part for host in hosts for part in ("--host", host)),
            "--env",
            env,
            "--placeholder",
            placeholder,
            "--command",
            command,
        )

    async def custom_placeholders(self, *, sandbox: str) -> list[str]:
        """``sbx secret ls`` has no JSON. The custom rows follow a ``CUSTOM
        SECRETS`` header, with the placeholder in the fourth column."""
        output = await self._run("secret", "ls", "--sandbox", sandbox)
        placeholders: list[str] = []
        custom = False
        for line in output.splitlines():
            if line.startswith("CUSTOM SECRETS"):
                custom = True
                continue
            columns = re.split(r"\s{2,}", line.strip())
            if custom and len(columns) >= 4 and columns[0] == sandbox:
                placeholders.append(columns[3])
        return placeholders

    async def remove_secret(self, service: str, *, sandbox: str) -> None:
        # Without -f the CLI waits for a confirmation.
        await self._run("secret", "rm", "-f", service, "--sandbox", sandbox)

    async def remove_custom_secret(self, *, sandbox: str, placeholder: str) -> None:
        await self._run("secret", "rm", "-f", "--placeholder", placeholder, "--sandbox", sandbox)

    async def load_template(self, archive: Path) -> None:
        await self._run("template", "load", str(archive))

    async def remove_template(self, template: str) -> None:
        await self._run("template", "rm", "--force", template)

    async def sandbox_count(self) -> int:
        output = await self._run("ls", "--json")
        try:
            payload = json.loads(output)
            # Go writes an empty list as null.
            return len(payload["sandboxes"] or [])
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise DockerSbxTransportError(
                f"sbx ls returned an unreadable sandbox list: {output.strip()!r}"
            ) from error

    async def check_ssh_endpoint(self) -> None:
        """The gateway tunnel needs the daemon's SSH endpoint. The CLI reads the
        endpoint's feature flag from its cache, so a container without the
        cache sees it as off. The daemon sends its banner for any name."""
        output = await self._run("ssh", "proxy", "drukbox-diagnose.sbx")
        if not output.startswith("SSH-"):
            raise DockerSbxTransportError(f"sbx ssh proxy sent no SSH banner: {output.strip()!r}")

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
            if _NOT_FOUND_RE.search(detail):
                raise DockerSbxNotFoundError(detail)
            raise DockerSbxTransportError(detail)
        return stdout.decode()
