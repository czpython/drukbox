import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ResourceNotFoundError
from host_secrets import catalog
from host_secrets.catalog import CATALOG
from host_secrets.exceptions import (
    SecretDeliveryFailedError,
    SecretDeliveryUnsupportedError,
    UnknownSecretServiceError,
)
from host_secrets.placeholder import Placeholder
from hosts.models import HostStatus
from hosts.service import HostService, utc_now
from providers.capabilities import SecretInjectionCapability, resolve_capability
from providers.exceptions import CapabilityUnsupportedError, ProviderError
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

        placeholder = Placeholder.mint(host.id, name)
        entry["placeholder_fingerprint"] = placeholder.fingerprint
        host.secrets[name] = entry
        host.updated_at = utc_now()
        if host.status == HostStatus.ACTIVE.value:
            try:
                provider = resolve_capability(
                    get_vm_provider(host.provider), SecretInjectionCapability
                )
            except CapabilityUnsupportedError as exc:
                raise SecretDeliveryUnsupportedError(
                    f"provider {host.provider!r} cannot deliver secrets to a running host"
                ) from exc
            if not provider.uses_secrets_exchange:
                raise SecretDeliveryUnsupportedError(
                    f"provider {host.provider!r} delivers at its own edge, and no adapter does yet"
                )
            service = catalog.service(name, entry)
            if not service["endpoint_var"]:
                raise SecretDeliveryUnsupportedError(
                    f"{name!r} has no base URL variable, so the exchange cannot route it"
                )
            try:
                await provider.put_secret(vm=host.name, service=service, value=str(placeholder))
            except ProviderError as exc:
                raise SecretDeliveryFailedError(str(exc)) from exc
        await self.session.commit()
