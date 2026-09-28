import contextlib
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import ClassVar, Self

import asyncssh

from providers import environment
from providers.base import VMCreateResult, VMProvider
from providers.capabilities import TemplateCapability
from providers.docker.api import DockerAPI
from providers.docker.exceptions import DockerProviderError
from providers.docker.images import build_derived_image
from providers.exceptions import (
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from providers.ssh_keys import generate_ed25519_keypair

from .api import SbxCLI
from .exceptions import DockerSbxNotFoundError, DockerSbxProviderError
from .process import SbxExecProcess
from .secrets import SbxInjection
from .settings import DockerSbxSettings


class DockerSbxProvider(VMProvider, TemplateCapability):
    name: ClassVar[str] = "docker-sbx"
    diagnose_hint: ClassVar[str] = "check_sandboxd_is_running_and_logged_in"
    gateway_process_class = SbxExecProcess
    supports_tailnet: ClassVar[bool] = False
    # sbx spends about 3 seconds on startup per call.
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
        # A workspace is mounted into its box, so the value files live beside them.
        self.secrets_root = settings.workspace_root / "secrets"
        self.secrets = SbxInjection(api, self.secrets_root)

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
        # A script here is a caller defect: supports_tailnet is False.
        if setup_script:
            raise ProviderCommandError(
                "docker-sbx provider runs sandboxes locally and does not "
                "support Tailscale networking"
            )

        caller_env = env or {}
        try:
            environment.get_persist(caller_env)
        except ValueError as exc:
            raise ProviderCommandError(str(exc)) from exc

        private_key, public_key = generate_ed25519_keypair()
        workspace = self._workspace(name)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
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
            # The CLI can fail after the daemon made the sandbox.
            with contextlib.suppress(DockerSbxProviderError):
                await self.api.remove_sandbox(name)
            self._remove_sandbox_files(name)
            raise ProviderTransportError(str(exc)) from exc

        username = self.settings.ssh_username
        home = "/root" if username == "root" else f"/home/{username}"
        owner = shlex.quote(username)
        script = "\n".join(
            [
                "set -euo pipefail",
                f"install -d -m 700 -o {owner} -g {owner} {home}/.ssh",
                f"printf '%s\\n' {shlex.quote(public_key)} > {home}/.ssh/authorized_keys",
                f"chmod 600 {home}/.ssh/authorized_keys",
                f"chown {owner}:{owner} {home}/.ssh/authorized_keys",
                # The runtime takes no environment at create time. pam_env reads this file.
                *environment.get_persist(caller_env),
                "",
            ]
        )
        try:
            await self.api.run_bootstrap(name, script)
        except DockerSbxProviderError as exc:
            with contextlib.suppress(DockerSbxProviderError):
                await self.api.remove_sandbox(name)
            self._remove_sandbox_files(name)
            raise ProviderTransportError(str(exc)) from exc

        # Callers arrive through the gateway. The service fills the coordinates in.
        return VMCreateResult(
            provider_id=name,
            name=name,
            ssh_username=username,
            private_key=private_key,
            public_key=public_key,
        )

    async def delete_vm(self, name: str) -> None:
        try:
            await self.api.remove_sandbox(name)
        except DockerSbxNotFoundError as exc:
            self._remove_sandbox_files(name)
            raise ProviderNotFoundError(f"sandbox '{name}' was not found") from exc
        except DockerSbxProviderError as exc:
            # The sandbox still runs on its workspace. The row stays for a retry.
            raise ProviderTransportError(str(exc)) from exc

        self._remove_sandbox_files(name)

    async def open_gateway_tunnel(self, name: str) -> asyncssh.SSHClientConnection:
        # sandboxd authenticates the OS user on its local socket, and sbx itself
        # trusts the host key on first use. No key crosses a network. asyncssh
        # raises ValueError when the local login name is unknown.
        try:
            return await asyncssh.connect(
                f"{name}.sbx",
                username=self.settings.ssh_username,
                proxy_command=["env", "SBX_NO_TELEMETRY=1", "sbx", "ssh", "proxy", f"{name}.sbx"],
                known_hosts=None,
                config=None,
                client_keys=None,
                agent_path=None,
                preferred_auth="none",
            )
        except (OSError, ValueError, asyncssh.Error) as exc:
            raise ProviderTransportError(f"sbx could not open a tunnel: {exc}") from exc

    async def build_template_image(
        self,
        *,
        base_image: str,
        setup_script: str,
        label: str,
    ) -> str:
        image = await build_derived_image(
            self.docker,
            base_image=base_image,
            setup_script=setup_script,
        )
        # sbx keeps its own image store. The Docker image only carries the build.
        try:
            with tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "template.tar"
                await self.docker.save_image(image, archive)
                await self.api.load_template(archive)
            await self.docker.remove_image(image)
        except (OSError, DockerProviderError, DockerSbxProviderError) as exc:
            raise ProviderTransportError(str(exc)) from exc
        return image

    async def delete_template_image(self, image: str) -> None:
        try:
            await self.api.remove_template(image)
        except DockerSbxNotFoundError as exc:
            raise ProviderNotFoundError(f"sbx template '{image}' was not found") from exc
        except DockerSbxProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

    async def diagnose(self) -> str:
        return f"sandboxd reachable, {await self.api.sandbox_count()} sandbox(es)"

    async def aclose(self) -> None:
        await self.docker.aclose()

    def _workspace(self, name: str) -> Path:
        return self.settings.workspace_root / name

    def _remove_sandbox_files(self, name: str) -> None:
        # An error here must not block the host deletion.
        shutil.rmtree(self._workspace(name), ignore_errors=True)
        shutil.rmtree(self.secrets_root / name, ignore_errors=True)
