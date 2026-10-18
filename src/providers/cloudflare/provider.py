from typing import ClassVar, Self

from core.settings import get_settings
from providers import environment
from providers.base import VMCreateResult, VMProvider

from .api import CloudflareAPI
from .settings import CloudflareSettings


class CloudflareProvider(VMProvider):
    name: ClassVar[str] = "cloudflare"
    diagnose_hint: ClassVar[str] = "check_cloudflare_worker_and_tailscale_settings"
    # The container has no public SSH address.
    tailnet = True
    supports_instance_type = True

    def __init__(
        self, api: CloudflareAPI, settings: CloudflareSettings, *, service_label: str
    ) -> None:
        self.api = api
        self.settings = settings
        self.service_label = service_label

    @classmethod
    def from_settings(cls) -> Self:
        settings = CloudflareSettings()  # pyright: ignore[reportCallIssue]
        return cls(CloudflareAPI(settings), settings, service_label=get_settings().service_label)

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
        await self.api.create_sandbox(
            name,
            image=image,
            instance=instance_type or self.settings.instance_type,
            script=environment.get_cloud_init(setup_script or "", env),
            label=self.service_label,
        )
        return VMCreateResult(provider_id=name, name=name, ssh_username="root")

    async def delete_vm(self, name: str) -> None:
        await self.api.delete_sandbox(name)

    async def diagnose(self) -> str:
        return await self.api.diagnose()

    async def aclose(self) -> None:
        await self.api.aclose()
