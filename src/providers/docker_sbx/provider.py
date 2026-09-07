import contextlib
import shlex
import shutil
from pathlib import Path
from typing import ClassVar, Self

from providers import environment
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
from .secrets import SbxInjection
from .settings import DockerSbxSettings


def _bootstrap_script(*, public_key: str, env: dict[str, str], ssh_username: str) -> str:
    home = "/root" if ssh_username == "root" else f"/home/{ssh_username}"
    owner = shlex.quote(ssh_username)
    lines = [
        "set -euo pipefail",
        f"install -d -m 700 -o {owner} -g {owner} {home}/.ssh",
        f"printf '%s\\n' {shlex.quote(public_key)} > {home}/.ssh/authorized_keys",
        f"chmod 600 {home}/.ssh/authorized_keys",
        f"chown {owner}:{owner} {home}/.ssh/authorized_keys",
    ]
    # The runtime takes no environment at create time. pam_env reads this file.
    return "\n".join([*lines, *environment.get_persist(env)]) + "\n"


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

        try:
            script = _bootstrap_script(
                public_key=public_key,
                env=caller_env,
                ssh_username=self.settings.ssh_username,
            )
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
            ssh_username=self.settings.ssh_username,
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
        )

    async def delete_template_image(self, image: str) -> None:
        await remove_derived_image(self.docker, image)

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
