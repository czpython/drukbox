import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ResourceNotFoundError
from host_secrets.catalog import CATALOG, describe
from host_secrets.exceptions import SecretDeliveryUnsupportedError, UnknownSecretServiceError
from host_secrets.placeholder import mint
from hosts.models import Host, HostStatus
from hosts.service import HostService, utc_now
from providers.capabilities import SecretInjectionCapability, resolve_capability
from providers.exceptions import CapabilityUnsupportedError
from providers.registry import get_vm_provider


class HostSecretService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.host_service = HostService(session)

    async def register_secret(
        self,
        *,
        host_id: uuid.UUID,
        name: str,
        entry: dict[str, Any],
    ) -> None:
        host = await self.host_service.get_host_for_update(host_id)
        if not host:
            raise ResourceNotFoundError("host not found")

        if "host" not in entry and name not in CATALOG:
            raise UnknownSecretServiceError(f"unknown secret service {name!r}")

        placeholder, entry["placeholder_sha256"] = mint(host.id, name)
        host.secrets[name] = entry
        host.updated_at = utc_now()
        if host.status == HostStatus.ACTIVE.value:
            await deliver(host, name, entry, placeholder)
        await self.session.commit()


async def deliver(host: Host, name: str, entry: dict[str, Any], placeholder: str) -> None:
    try:
        provider = resolve_capability(get_vm_provider(host.provider), SecretInjectionCapability)
    except CapabilityUnsupportedError as exc:
        raise SecretDeliveryUnsupportedError(
            f"provider {host.provider!r} cannot deliver secrets to a running host"
        ) from exc
    # A provider that injects at its own edge takes the secret itself, and
    # writing the environment it returns into the VM is that adapter's work.
    if not provider.injects_at_own_edge:
        await provider.put_secret(vm=host.name, service=describe(name, entry), value=placeholder)
