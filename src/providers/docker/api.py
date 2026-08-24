import asyncio
import os
import tempfile
from pathlib import Path

from .exceptions import DockerImageNotFoundError, DockerTransportError, DockerVMNotFoundError

_MAX_ERROR_DETAIL_CHARS = 8_000


class DockerCLI:
    """Thin async wrapper over the local ``docker`` command-line interface.

    Every method shells out to ``docker`` through ``create_subprocess_exec`` so
    the subprocess boundary stays in one place. It talks to whatever daemon the
    CLI is configured for (Docker Desktop, OrbStack, Colima, a remote
    ``DOCKER_HOST``); the CLI owns that resolution, not drukbox.
    """

    async def run_container(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str],
        labels: dict[str, str],
    ) -> str:
        # Publish the in-container sshd on a random loopback host port: the
        # sandbox is reachable from the host that runs drukbox, never from the
        # network. The per-VM key remains the auth boundary.
        args = ["run", "--detach", "--name", name, "--publish", "127.0.0.1::22"]
        for key, value in labels.items():
            args.extend(["--label", f"{key}={value}"])
        env_file = _write_env_file(env) if env else None
        if env_file:
            # --env-file keeps caller secrets off argv (world-readable via ps/proc
            # for the lifetime of `docker run`); only the path is passed, and the
            # file is removed in the finally once docker has read it.
            args.extend(["--env-file", env_file])
        args.append(image)
        try:
            return (await self._run(*args)).strip()
        finally:
            if env_file:
                os.unlink(env_file)

    async def published_ssh_port(self, name: str) -> int:
        output = await self._run("port", name, "22/tcp")
        # `docker port` prints one binding per line, e.g. "127.0.0.1:49160".
        # An empty result means sshd's port never bound — the container exited.
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines:
            raise DockerTransportError(f"container {name!r} published no SSH port")
        try:
            return int(lines[0].rsplit(":", 1)[1])
        except (ValueError, IndexError) as exc:
            # Keep unparsable output a DockerProviderError so the provider's
            # create_vm cleanup (keyed on it) still removes the container.
            raise DockerTransportError(
                f"container {name!r} published an unparsable SSH port: {lines[0]!r}"
            ) from exc

    async def remove_container(self, name: str) -> None:
        await self._run("rm", "--force", "--volumes", name)

    async def build_image(self, tag: str, context_dir: Path) -> None:
        await self._run("build", "--tag", tag, str(context_dir))

    async def remove_image(self, tag: str) -> None:
        await self._run("image", "rm", tag)

    async def push_image(self, tag: str) -> None:
        await self._run("push", tag)

    async def login(self, registry: str, username: str, password: str) -> None:
        await self._run(
            "login",
            registry,
            "--username",
            username,
            "--password-stdin",
            stdin=f"{password}\n".encode(),
        )

    async def server_version(self) -> str:
        return (await self._run("version", "--format", "{{.Server.Version}}")).strip()

    async def _run(self, *args: str, stdin: bytes | None = None) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            # Missing binary (FileNotFoundError) or e.g. a non-executable one
            # (PermissionError) — translate both so a launch failure can't
            # escape the provider boundary as a raw OSError.
            raise DockerTransportError(f"docker CLI could not be started: {error}") from error
        stdout, stderr = await process.communicate(stdin)
        if process.returncode != 0:
            detail = "\n".join(
                output.decode(errors="replace") for output in (stdout, stderr) if output
            ).strip()
            if detail:
                detail = detail[-_MAX_ERROR_DETAIL_CHARS:]
            else:
                detail = f"docker {args[0]} exited {process.returncode}"
            if "no such container" in detail.lower():
                raise DockerVMNotFoundError(detail)
            if "no such image" in detail.lower():
                raise DockerImageNotFoundError(detail)
            raise DockerTransportError(detail)
        return stdout.decode(errors="replace")


def _write_env_file(env: dict[str, str]) -> str:
    """Write ``env`` to a fresh 0600 temp file in docker --env-file format.

    Validates before creating the file (so a bad entry can't leak one): a NUL or
    newline in a value would inject extra env entries into the line-based format,
    and neither is representable in it.
    """
    lines: list[str] = []
    for key, value in env.items():
        if any(ch in key or ch in value for ch in "\x00\r\n"):
            raise DockerTransportError(
                f"docker env entry {key!r} cannot contain NUL or newline characters"
            )
        lines.append(f"{key}={value}\n")
    fd, path = tempfile.mkstemp(prefix="drukbox-docker-env-", suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("".join(lines))
    return path
