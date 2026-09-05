import contextlib
import shlex
import shutil
from pathlib import Path
from typing import ClassVar, Self

from providers.base import VMCreateResult, VMProvider
from providers.capabilities import TemplateCapability
from providers.docker.api import DockerAPI
from providers.docker.images import build_derived_image, remove_derived_image
from providers.exceptions import (
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from providers.ssh_keys import generate_ed25519_keypair

from .api import SbxCLI
from .exceptions import DockerSbxNotFoundError, DockerSbxProviderError
from .process import SbxExecProcess
from .settings import DockerSbxSettings

# The /etc/environment format has one entry on each line. A NUL or a newline
# in a value can add unwanted entries. Thus these characters are not permitted
# in a value. The schema (hosts.schemas) validates the keys before this point.
_UNSAFE_ENV_VALUE_CHARS = frozenset("\x00\r\n")


def _bootstrap_script(*, public_key: str, env: dict[str, str], ssh_username: str) -> str:
    """Make the root script that prepares SSH access to a new sandbox."""
    home = "/root" if ssh_username == "root" else f"/home/{ssh_username}"
    owner = shlex.quote(ssh_username)
    lines = [
        "set -euo pipefail",
        f"install -d -m 700 -o {owner} -g {owner} {home}/.ssh",
        f"printf '%s\\n' {shlex.quote(public_key)} > {home}/.ssh/authorized_keys",
        f"chmod 600 {home}/.ssh/authorized_keys",
        f"chown {owner}:{owner} {home}/.ssh/authorized_keys",
    ]
    # pam_env reads /etc/environment and gives the caller environment to SSH
    # sessions. The sandbox runtime cannot receive environment variables at
    # create time. This file is the only path.
    for key, value in env.items():
        lines.append(f"printf '%s\\n' {shlex.quote(f'{key}={value}')} >> /etc/environment")
    return "\n".join(lines) + "\n"


class DockerSbxProvider(VMProvider, TemplateCapability):
    name: ClassVar[str] = "docker-sbx"
    diagnose_hint: ClassVar[str] = "check_sandboxd_is_running_and_logged_in"
    # Sandboxes have no dialable sshd; the gateway serves them, and there is
    # no path onto the tailnet.
    gateway_process_class = SbxExecProcess
    supports_tailnet: ClassVar[bool] = False
    # Each sbx invocation spends approximately 3 seconds on CLI startup work
    # before the command runs. The default 5-second probe budget fails on a
    # healthy daemon.
    diagnose_timeout_seconds: ClassVar[float] = 15.0

    def __init__(
        self,
        api: SbxCLI,
        settings: DockerSbxSettings,
        *,
        docker: DockerAPI,
    ) -> None:
        self.api = api
        self.settings = settings
        self.docker = docker

    @classmethod
    def from_settings(cls) -> Self:
        return cls(
            SbxCLI(),
            DockerSbxSettings(),  # pyright: ignore[reportCallIssue]
            docker=DockerAPI(),
        )

    @property
    def default_image(self) -> str:
        return self.settings.default_image

    @property
    def bootstrap_ssh_timeout_seconds(self) -> float:
        return self.settings.bootstrap_ssh_timeout_seconds

    async def create_vm(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str] | None = None,
        setup_script: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
    ) -> VMCreateResult:
        # The service does not send a setup script, because supports_tailnet
        # is False. A script here shows a defect in the caller. Stop with an
        # error. Do not start a sandbox that cannot obey the script.
        if setup_script:
            raise ProviderCommandError(
                "docker-sbx provider runs sandboxes locally and does not "
                "support Tailscale networking"
            )

        caller_env = env or {}
        if unsafe_keys := sorted(
            key for key, value in caller_env.items() if _UNSAFE_ENV_VALUE_CHARS.intersection(value)
        ):
            raise ProviderCommandError(
                f"env values must not contain NUL or newline characters: {', '.join(unsafe_keys)}"
            )

        private_key, public_key = generate_ed25519_keypair()
        workspace = self._workspace(name)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # The workspace root can be not writable (no bind mount, or a
            # read-only filesystem). The service cannot classify a raw
            # OSError, thus the error becomes a provider error here.
            raise ProviderTransportError(f"cannot create sandbox workspace: {exc}") from exc

        try:
            await self.api.create_sandbox(
                name=name,
                template=image,
                workspace=str(workspace),
                cpus=self.settings.cpus,
                memory=self.settings.memory,
            )
        except DockerSbxProviderError as exc:
            # The CLI can stop after the daemon makes the sandbox. Thus a
            # failed create also tries to remove the sandbox. An error in this
            # cleanup must not hide the first error. The janitor removes the
            # sandbox by name if the cleanup fails.
            with contextlib.suppress(DockerSbxProviderError):
                await self.api.remove_sandbox(name)
            self._remove_workspace(name)
            raise ProviderTransportError(str(exc)) from exc

        try:
            # The template starts sshd with an empty authorized_keys file. The
            # sandbox accepts SSH only after this key is in the file.
            script = _bootstrap_script(
                public_key=public_key,
                env=caller_env,
                ssh_username=self.settings.ssh_username,
            )
            await self.api.run_bootstrap(name, script)
        except DockerSbxProviderError as exc:
            with contextlib.suppress(DockerSbxProviderError):
                await self.api.remove_sandbox(name)
            self._remove_workspace(name)
            raise ProviderTransportError(str(exc)) from exc

        # A sandbox has no reachable address of its own: callers arrive
        # through the gateway, and the service fills the coordinates in.
        return VMCreateResult(
            provider_id=name,
            name=name,
            ssh_username=self.settings.ssh_username,
            private_key=private_key,
            public_key=public_key,
        )

    async def delete_vm(self, name: str) -> None:
        try:
            await self.api.remove_sandbox(name)
        except DockerSbxNotFoundError as exc:
            # The sandbox is not there, but its workspace can be. Remove the
            # workspace also.
            self._remove_workspace(name)
            raise ProviderNotFoundError(f"sandbox '{name}' was not found") from exc
        except DockerSbxProviderError as exc:
            # Keep the workspace. The sandbox can continue to operate on it.
            # HostService keeps the record and can try the deletion again.
            raise ProviderTransportError(str(exc)) from exc

        self._remove_workspace(name)

    async def build_template_image(
        self,
        *,
        base_image: str,
        setup_script: str,
        label: str,
    ) -> str:
        return await build_derived_image(
            self.docker,
            base_image=base_image,
            setup_script=setup_script,
            label=label,
        )

    async def delete_template_image(self, image: str) -> None:
        await remove_derived_image(self.docker, image)

    async def diagnose(self) -> str:
        # The sandbox list is one fast check of the CLI, the daemon
        # connection, and the Docker login.
        return f"sandboxd reachable, {await self.api.sandbox_count()} sandbox(es)"

    async def aclose(self) -> None:
        await self.docker.aclose()

    def _workspace(self, name: str) -> Path:
        return self.settings.workspace_root / name

    def _remove_workspace(self, name: str) -> None:
        # The workspace is temporary data for one sandbox. An error here must
        # not block the host deletion.
        shutil.rmtree(self._workspace(name), ignore_errors=True)
