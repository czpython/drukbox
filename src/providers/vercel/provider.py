from datetime import timedelta
from typing import ClassVar, Self

from core.settings import get_settings
from providers import environment
from providers.base import VMCreateResult, VMProvider
from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
)

from .api import VercelAPI
from .settings import VercelSettings


class VercelProvider(VMProvider):
    name: ClassVar[str] = "vercel"
    diagnose_hint: ClassVar[str] = "check_vercel_and_tailscale_settings"
    supports_instance_type = True

    def __init__(self, api: VercelAPI, settings: VercelSettings, *, service_label: str) -> None:
        self.api = api
        self.settings = settings
        self.service_label = service_label
        self.max_lifetime = timedelta(seconds=settings.max_lifetime_seconds)

    @classmethod
    def from_settings(cls) -> Self:
        settings = VercelSettings()  # pyright: ignore[reportCallIssue]
        return cls(VercelAPI(settings), settings, service_label=get_settings().service_label)

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
            raise ProviderCommandError("Vercel requires TAILSCALE_ENABLED=true")
        try:
            vcpus = int(instance_type or self.settings.vcpus)
        except ValueError:
            raise ProviderCommandError("Vercel instance_type must be a vCPU count") from None
        try:
            session_id = await self.api.create_sandbox(
                name, image=image, vcpus=vcpus, label=self.service_label
            )
            await self.api.bootstrap(session_id, environment.get_cloud_init(setup_script, env))
        except (ProviderAuthError, ProviderNotFoundError) as exc:
            raise ProviderCommandError(str(exc)) from exc
        return VMCreateResult(provider_id=session_id, name=name, ssh_username="root")

    async def delete_vm(self, name: str) -> None:
        await self.api.delete_sandbox(name)

    async def diagnose(self) -> str:
        if not get_settings().tailscale_enabled:
            raise ProviderCommandError("Vercel requires TAILSCALE_ENABLED=true")
        return await self.api.diagnose()

    async def aclose(self) -> None:
        await self.api.aclose()
