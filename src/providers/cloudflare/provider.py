import contextlib
from typing import ClassVar, Self

from core.settings import get_settings
from providers import environment
from providers.base import VMCreateResult, VMProvider
from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderError,
    ProviderTransportError,
)

from .api import CloudflareAPI
from .settings import CloudflareSettings


class CloudflareProvider(VMProvider):
    name: ClassVar[str] = "cloudflare"
    diagnose_hint: ClassVar[str] = "check_cloudflare_worker_and_tailscale_settings"
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
        if not setup_script:
            raise ProviderCommandError("Cloudflare requires TAILSCALE_ENABLED=true")
        size = instance_type or self.settings.instance_type
        if size not in {"lite", "standard-1", "standard-2", "standard-3", "standard-4"}:
            raise ProviderCommandError("Unsupported Cloudflare instance_type")
        try:
            script = environment.get_cloud_init(setup_script, env)
        except ValueError as exc:
            raise ProviderCommandError(str(exc)) from exc
        try:
            await self.api.create_sandbox(
                name, image=image, instance=size, script=script, label=self.service_label
            )
        except ProviderAuthError as exc:
            raise ProviderCommandError("Cloudflare Worker authentication failed") from exc
        except ProviderTransportError:
            with contextlib.suppress(ProviderError):
                await self.api.delete_sandbox(name)
            raise
        return VMCreateResult(provider_id=name, name=name, ssh_username="root")

    async def delete_vm(self, name: str) -> None:
        await self.api.delete_sandbox(name)

    async def diagnose(self) -> str:
        if not get_settings().tailscale_enabled:
            raise ProviderCommandError("Cloudflare requires TAILSCALE_ENABLED=true")
        return await self.api.diagnose()

    async def aclose(self) -> None:
        await self.api.aclose()
