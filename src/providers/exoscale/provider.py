from typing import ClassVar, Self

from core.settings import get_settings
from providers import environment
from providers.base import VMCreateResult, VMProvider
from providers.exceptions import ProviderNotFoundError, ProviderTransportError
from providers.ssh_keys import generate_ed25519_keypair

from .api import ExoscaleAPI
from .exceptions import ExoscaleProviderError
from .settings import ExoscaleSettings


class ExoscaleProvider(VMProvider):
    name: ClassVar[str] = "exoscale"
    diagnose_hint: ClassVar[str] = "check_exoscale_api_credentials_and_zone"
    supports_instance_type = True
    supports_disk_gb = True

    def __init__(
        self,
        api: ExoscaleAPI,
        settings: ExoscaleSettings,
        *,
        service_label: str = "drukbox",
    ) -> None:
        self.api = api
        self.settings = settings
        self._service_label = service_label

    @classmethod
    def from_settings(cls) -> Self:
        core = get_settings()
        exoscale_settings = ExoscaleSettings()  # pyright: ignore[reportCallIssue]
        return cls(
            ExoscaleAPI.from_settings(exoscale_settings),
            exoscale_settings,
            service_label=core.service_label,
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
        key_name = f"drukbox-{name}"
        private_key, public_key = generate_ed25519_keypair()
        labels = {"managed-by": self._service_label, "drukbox-host-name": name}
        try:
            await self.api.ensure_ssh_key(name=key_name, public_key=public_key, labels=labels)
        except ExoscaleProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

        user_data = environment.cloud_init(setup_script or "", env)
        try:
            instance_id = await self.api.create_instance(
                name=name,
                image=image,
                instance_type=instance_type,
                disk_gb=disk_gb or self.settings.disk_gb,
                ssh_key_name=key_name,
                user_data=user_data,
                labels=labels,
            )
        except ExoscaleProviderError as exc:
            await self.api.delete_ssh_key(key_name)
            raise ProviderTransportError(str(exc)) from exc

        try:
            ssh_host = await self.api.wait_for_running_with_ip(instance_id)
        except ExoscaleProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

        return VMCreateResult(
            provider_id=instance_id,
            name=name,
            ssh_port=22,
            ssh_host=ssh_host,
            ssh_username=self.settings.ssh_username,
            private_key=private_key,
        )

    async def delete_vm(self, name: str) -> None:
        # Delete the key first and unconditionally: delete_ssh_key is idempotent
        # (a missing key is a no-op) and deleting it doesn't affect a running
        # instance, so the key can't be stranded behind the instance-not-found
        # short-circuit when an earlier teardown removed the instance but not the
        # key. delete_instance treats a 404 as success, so a None lookup is the
        # only not-found signal.
        try:
            await self.api.delete_ssh_key(f"drukbox-{name}")
            instance_id = await self.api.find_instance_id_by_name(name)
            if instance_id is None:
                raise ProviderNotFoundError(f"exoscale VM '{name}' was not found")
            await self.api.delete_instance(instance_id)
        except ExoscaleProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

    async def aclose(self) -> None:
        await self.api.aclose()

    async def diagnose(self) -> str:
        count = await self.api.list_instances_count()
        return f"zone={self.settings.zone} instances={count}"
