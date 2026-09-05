from typing import ClassVar, Self

from core.settings import get_settings
from providers import environment
from providers.base import VMCreateResult, VMProvider
from providers.exceptions import ProviderNotFoundError, ProviderTransportError
from providers.ssh_keys import generate_ed25519_keypair

from .api import HetznerAPI
from .exceptions import HetznerProviderError
from .settings import HetznerSettings


class HetznerProvider(VMProvider):
    name: ClassVar[str] = "hetzner"
    diagnose_hint: ClassVar[str] = "check_hetzner_api_token_and_location"
    # instance_type maps onto Hetzner's server type; root disk size is fixed
    # by the server type, so disk_gb stays unsupported.
    supports_instance_type = True

    def __init__(
        self,
        api: HetznerAPI,
        settings: HetznerSettings,
        *,
        service_label: str = "drukbox",
    ) -> None:
        self.api = api
        self.settings = settings
        self._service_label = service_label

    @classmethod
    def from_settings(cls) -> Self:
        core = get_settings()
        hetzner_settings = HetznerSettings()  # pyright: ignore[reportCallIssue]
        return cls(
            HetznerAPI.from_settings(hetzner_settings),
            hetzner_settings,
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
        # Always mint a per-VM key, in both networking modes: Hetzner servers
        # have a public IP and no firewall by default, and attaching a key both
        # locks port 22 and stops Hetzner emailing a root password (which it
        # does when no key is attached). Tailscale, when on, is layered on by
        # the caller's bootstrap script, so the provider needs no branch.
        key_name = f"drukbox-{name}"
        private_key, public_key = generate_ed25519_keypair()
        labels = {"managed-by": self._service_label, "drukbox-host-name": name}
        try:
            await self.api.ensure_ssh_key(name=key_name, public_key=public_key, labels=labels)
        except HetznerProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

        user_data = environment.cloud_init(setup_script or "", env)
        try:
            server_id = await self.api.create_server(
                name=name,
                image=image,
                server_type=instance_type,
                ssh_key_name=key_name,
                user_data=user_data,
                labels=labels,
            )
        except HetznerProviderError as exc:
            await self.api.delete_ssh_key(key_name)
            raise ProviderTransportError(str(exc)) from exc

        try:
            ssh_host = await self.api.wait_for_running_with_ip(server_id)
        except HetznerProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

        return VMCreateResult(
            provider_id=server_id,
            name=name,
            ssh_port=22,
            ssh_host=ssh_host,
            ssh_username=self.settings.ssh_username,
            private_key=private_key,
        )

    async def delete_vm(self, name: str) -> None:
        # Delete the key first and unconditionally: delete_ssh_key is idempotent
        # (a missing key is a no-op) and deleting it doesn't affect a running
        # server, so the key can't be stranded behind the server-not-found
        # short-circuit when an earlier teardown removed the server but not the
        # key. delete_server treats a 404 as success, so a None lookup is the
        # only not-found signal.
        try:
            await self.api.delete_ssh_key(f"drukbox-{name}")
            server_id = await self.api.find_server_id_by_name(name)
            if server_id is None:
                raise ProviderNotFoundError(f"hetzner VM '{name}' was not found")
            await self.api.delete_server(server_id)
        except HetznerProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

    async def aclose(self) -> None:
        await self.api.aclose()

    async def diagnose(self) -> str:
        count = await self.api.count_servers()
        return f"location={self.settings.location} servers={count}"
