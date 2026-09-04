import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ResourceNotFoundError
from host_secrets.catalog import resolve_secret_config
from host_secrets.schemas import SecretEntry
from hosts.service import HostService, utc_now


class HostSecretService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.host_service = HostService(session)

    async def register_secret(
        self,
        *,
        host_id: uuid.UUID,
        name: str,
        entry: SecretEntry,
    ) -> None:
        host = await self.host_service.get_host_for_update(host_id)
        if not host:
            raise ResourceNotFoundError("host not found")

        resolve_secret_config(name, entry)
        host.secrets[name] = entry
        host.updated_at = utc_now()
        await self.session.commit()
