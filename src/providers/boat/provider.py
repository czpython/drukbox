import contextlib
import shlex
from typing import ClassVar, Self

from core.settings import get_settings
from providers import environment
from providers.base import VMCreateResult, VMProvider
from providers.exceptions import ProviderCommandError, ProviderTransportError
from providers.ssh_keys import generate_ed25519_keypair

from .api import BoatAPI
from .exceptions import BoatError
from .settings import BoatSettings


class BoatProvider(VMProvider):
    name: ClassVar[str] = "boat"
    diagnose_hint: ClassVar[str] = "check_boat_api_token_and_sandbox_permissions"
    supports_tailnet = False
    supports_instance_type = True

    def __init__(self, api: BoatAPI, settings: BoatSettings, *, service_label: str) -> None:
        self.api = api
        self.settings = settings
        self.service_label = service_label

    @classmethod
    def from_settings(cls) -> Self:
        settings = BoatSettings()  # pyright: ignore[reportCallIssue]
        return cls(BoatAPI(settings), settings, service_label=get_settings().service_label)

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
        if setup_script or disk_gb:
            raise ProviderCommandError("Boat does not support Tailscale bootstrap or disk sizing")
        size = instance_type or self.settings.instance_type
        if size not in {"small", "default", "large"}:
            raise ProviderCommandError("Boat instance_type must be small, default, or large")
        try:
            script = environment.get_cloud_init("set -e", env)
        except ValueError as exc:
            raise ProviderCommandError(str(exc)) from exc
        private_key, public_key = generate_ed25519_keypair()
        resource_name = f"{self.service_label}:{name}"
        try:
            sandbox_id = await self.api.create_sandbox(
                resource_name, image=image, instance_type=size
            )
        except BoatError as exc:
            raise ProviderTransportError("Boat sandbox allocation failed") from exc
        try:
            await self.api.name_sandbox(sandbox_id, resource_name)
            await self.api.wait_ready(sandbox_id)
            await self.api.run_command(sandbox_id, f"sudo -n bash -e -c {shlex.quote(script)}")
            ssh = await self.api.configure_ssh(sandbox_id, public_key)
            host = ssh["machineIp"]
            port = 22
            if endpoint := ssh.get("sshEndpoint"):
                host, _, port_text = endpoint.rpartition(":")
                port = int(port_text)
            if not host:
                raise ProviderCommandError("Boat returned no SSH address")
            username = ssh["sshUser"]
        except (BoatError, ProviderCommandError, KeyError, TypeError, ValueError) as exc:
            with contextlib.suppress(BoatError):
                await self.api.delete_sandbox(sandbox_id)
            raise ProviderCommandError(f"Boat sandbox {sandbox_id} provisioning failed") from exc
        return VMCreateResult(
            provider_id=sandbox_id,
            name=name,
            ssh_host=host,
            ssh_port=port,
            ssh_username=username,
            private_key=private_key,
        )

    async def delete_vm(self, name: str) -> None:
        sandbox_id = await self.api.find_sandbox(f"{self.service_label}:{name}")
        await self.api.delete_sandbox(sandbox_id)

    async def diagnose(self) -> str:
        return await self.api.diagnose()

    async def aclose(self) -> None:
        await self.api.aclose()
