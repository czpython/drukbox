import uuid
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ResourceNotFoundError
from core.settings import Settings, get_settings
from hosts.exceptions import HostStateError
from hosts.models import HostStatus
from hosts.service import HostService
from http_proxies.exceptions import (
    HTTPProxyError,
    HTTPProxyExistsError,
    HTTPProxyNotFoundError,
    HTTPProxyUnsupportedError,
)
from providers.exceptions import (
    ProviderError,
    ProviderHttpProxyExistsError,
    ProviderHttpProxyNotFoundError,
    ProviderTargetVMNotFoundError,
    UnknownProviderError,
)
from providers.exe.provider import ExeProvider
from providers.registry import get_vm_provider

ATTACHABLE_HOST_STATUSES = frozenset(
    {
        HostStatus.BOOTSTRAPPING.value,
        HostStatus.ACTIVE.value,
    }
)


class HTTPProxyService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.host_service = HostService(session, settings=self.settings)

    def _exe_provider(self, provider_name: str | None = None) -> ExeProvider:
        try:
            provider = get_vm_provider(provider_name)
        except UnknownProviderError as exc:
            raise HTTPProxyUnsupportedError(str(exc)) from exc

        if provider.name != ExeProvider.name:
            raise HTTPProxyUnsupportedError(
                f"VM provider '{provider.name}' does not support account-bound HTTP proxies"
            )
        return cast(ExeProvider, provider)

    async def create_http_proxy(
        self,
        *,
        name: str,
        target: str,
        headers: dict[str, str],
    ) -> None:
        provider = self._exe_provider()

        try:
            await provider.create_http_proxy(name=name, target=target, headers=headers)
        except ProviderHttpProxyExistsError as exc:
            raise HTTPProxyExistsError("http proxy already exists") from exc
        except ProviderError as exc:
            raise HTTPProxyError("http proxy could not be created") from exc

    async def delete_http_proxy(self, name: str) -> None:
        provider = self._exe_provider()

        try:
            await provider.delete_http_proxy(name)
        except ProviderHttpProxyNotFoundError as exc:
            raise HTTPProxyNotFoundError("http proxy not found") from exc
        except ProviderError as exc:
            raise HTTPProxyError("http proxy could not be removed") from exc

    async def attach_http_proxy(self, name: str, host_id: uuid.UUID) -> None:
        host = await self.host_service.get_host_for_update(host_id)

        if host is None:
            raise ResourceNotFoundError("host not found")

        if host.status not in ATTACHABLE_HOST_STATUSES:
            raise HostStateError("host does not have a backing VM")

        provider = self._exe_provider(host.provider)

        try:
            await provider.attach_http_proxy(name, attach_vm=host.name)
        except ProviderTargetVMNotFoundError as exc:
            raise HostStateError("host does not have a backing VM") from exc
        except ProviderHttpProxyNotFoundError as exc:
            raise HTTPProxyNotFoundError("http proxy not found") from exc
        except ProviderError as exc:
            raise HTTPProxyError("http proxy could not be attached") from exc

    async def detach_http_proxy(self, name: str, host_id: uuid.UUID) -> None:
        host = await self.host_service.get_host_for_update(host_id)

        if host is None:
            raise ResourceNotFoundError("host not found")

        if host.status not in ATTACHABLE_HOST_STATUSES:
            raise HostStateError("host does not have a backing VM")

        provider = self._exe_provider(host.provider)

        try:
            await provider.detach_http_proxy(name, attach_vm=host.name)
        except ProviderTargetVMNotFoundError as exc:
            raise HostStateError("host does not have a backing VM") from exc
        except ProviderHttpProxyNotFoundError as exc:
            raise HTTPProxyNotFoundError("http proxy not found") from exc
        except ProviderError as exc:
            raise HTTPProxyError("http proxy could not be detached") from exc
