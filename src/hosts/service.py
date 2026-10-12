import asyncio
import logging
import pathlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import EllipsisType
from typing import Any

import httpx
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from core.database import async_session_factory
from core.exceptions import ResourceNotFoundError
from core.settings import Settings, get_settings
from gateway.settings import GatewaySettings
from host_secrets import catalog
from host_secrets.exceptions import SecretsProxyNotConfiguredError, SecretStaticError
from host_secrets.placeholder import Placeholder
from hosts.exceptions import (
    HostLeaseError,
    HostStateError,
    IdempotencyKeyConflictError,
    ProvisioningFailedError,
)
from hosts.models import Host, HostStatus, IdempotencyKey
from networking.tailscale import (
    DeviceDiscoveryTimeoutError,
    NetworkError,
    Tailscale,
)
from providers.base import VMProvider
from providers.environment import get_persist
from providers.exceptions import (
    ProviderCommandError,
    ProviderError,
    ProviderNotFoundError,
    ProviderTransportError,
    UnknownProviderError,
    UnsupportedSizingError,
)
from providers.registry import get_provider_names, get_vm_provider
from secrets_exchange.client import SecretsExchange
from secrets_exchange.secrets import IssuerError, Secret
from templates.exceptions import TemplateNotAvailableError, UnknownTemplateError
from templates.models import Template, TemplateStatus

logger = logging.getLogger(__name__)

_SANDBOX_BOOTSTRAP_SCRIPT = (
    pathlib.Path(__file__).resolve().parent / "scripts" / "sandbox_bootstrap.sh"
).read_text(encoding="utf-8")
DELETE_BLOCKED_STATUSES = frozenset(
    {
        HostStatus.PROVISIONING.value,
        HostStatus.CREATING_NETWORK.value,
        HostStatus.CREATING_VM.value,
    }
)
VM_BACKED_STATUSES = frozenset(
    {
        HostStatus.BOOTSTRAPPING.value,
        HostStatus.ACTIVE.value,
        HostStatus.ERROR.value,
    }
)
RENEWABLE_STATUSES = frozenset(
    {
        HostStatus.BOOTSTRAPPING.value,
        HostStatus.ACTIVE.value,
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class HostService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        *,
        tailscale: Tailscale | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        if tailscale:
            self.tailscale: Tailscale | None = tailscale
        elif self.settings.tailscale_enabled:
            self.tailscale = Tailscale.from_settings()
        else:
            self.tailscale = None

    def _default_lease_expires_at(self) -> datetime:
        return utc_now() + timedelta(seconds=self.settings.lease_default_ttl)

    async def get_or_create_host(
        self,
        *,
        service_account: str | None = None,
        env: dict[str, str],
        secrets: dict[str, dict[str, Any]] | None = None,
        image: str | None,
        template: uuid.UUID | None = None,
        expires_at: datetime | None | EllipsisType = ...,
        idempotency_key: str | None = None,
        provider: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
    ) -> Host:
        # Omission and explicit null must stay distinct until provisioning completes.
        if provider:
            registered = get_provider_names()
            if provider not in registered:
                available = ", ".join(sorted(registered))
                raise UnknownProviderError(f"unknown provider {provider!r}; available: {available}")

        if idempotency_key:
            existing = await self._lookup_idempotency_key(idempotency_key, service_account)
            if existing:
                return existing

        host: Host | None = None
        # Only requests for the provider defaults can claim a warm host.
        requested_provider = provider or self.settings.default_host_provider
        customized = env or secrets or image or template or instance_type or disk_gb
        if not customized and self.settings.get_pool_targets().get(requested_provider):
            host = await self._try_claim_pool_host(
                service_account=service_account, provider=requested_provider, expires_at=expires_at
            )
        if not host:
            host = await self.create_host(
                service_account=service_account,
                env=env,
                secrets=secrets,
                image=image,
                template=template,
                expires_at=expires_at,
                provider=provider,
                instance_type=instance_type,
                disk_gb=disk_gb,
            )

        if idempotency_key and not await self._record_idempotency_key(idempotency_key, host):
            logger.info(
                "idempotency: lost race on key=%s host_id=%s claimed_at=%s",
                idempotency_key,
                host.id,
                host.claimed_at,
            )
            await self._release_idempotency_loser(host)
            winner = await self._lookup_idempotency_key(idempotency_key, service_account)
            if not winner:
                raise HostStateError("idempotency race could not be resolved") from None
            return winner
        return host

    async def _try_claim_pool_host(
        self,
        *,
        service_account: str | None = None,
        provider: str,
        expires_at: datetime | None | EllipsisType,
    ) -> Host | None:
        # The conditional UPDATE gives concurrent claimants one winner.
        now = utc_now()
        candidates = (
            select(Host)
            .where(Host.provider == provider)
            .where(Host.pool_member.is_(True))
            .where(Host.claimed_at.is_(None))
            .where(Host.status == HostStatus.ACTIVE.value)
            .where(or_(Host.expires_at.is_(None), Host.expires_at > now))
            .where(or_(Host.lease_deadline.is_(None), Host.lease_deadline > now))
            .order_by(Host.created_at.asc())
            .limit(1)
        )
        if expires_at is not ...:
            if expires_at:
                candidates = candidates.where(
                    or_(Host.lease_deadline.is_(None), Host.lease_deadline >= expires_at)
                )
            else:
                candidates = candidates.where(Host.lease_deadline.is_(None))
        candidate = (await self.session.execute(candidates)).scalar_one_or_none()
        if not candidate:
            return
        expires_at = candidate.lease_expiry(expires_at, default=self._default_lease_expires_at())
        result = await self.session.execute(
            update(Host)
            .where(Host.id == candidate.id)
            .where(Host.claimed_at.is_(None))
            .values(
                service_account=service_account,
                claimed_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
            .returning(Host)
        )
        host = result.scalar_one_or_none()
        await self.session.commit()
        if host:
            logger.info("pool: claimed host_id=%s name=%s", host.id, host.name)
            return host

    async def create_host(
        self,
        *,
        service_account: str | None = None,
        env: dict[str, str],
        secrets: dict[str, dict[str, Any]] | None = None,
        image: str | None,
        template: uuid.UUID | None = None,
        expires_at: datetime | None | EllipsisType = ...,
        provider: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
        pool_member: bool = False,
    ) -> Host:
        vm = get_vm_provider(provider)
        if instance_type and not vm.supports_instance_type:
            raise UnsupportedSizingError(
                f"provider {vm.name!r} does not support a per-request instance_type"
            )
        if disk_gb and not vm.supports_disk_gb:
            raise UnsupportedSizingError(
                f"provider {vm.name!r} does not support a per-request disk_gb"
            )
        proxy = self.settings.secrets_proxy_url and self.settings.secrets_proxy_ca_file
        if secrets and not vm.secrets.needs_value and not proxy:
            raise SecretsProxyNotConfiguredError(
                "SECRETS_PROXY_URL and SECRETS_PROXY_CA_FILE must name the proxy that "
                "sandboxes dial and its certificate"
            )
        if template:
            image = await self._resolve_template_image(template_id=template, provider=vm.name)
        uid = uuid7()
        name = Host.build_name(uid)
        now = utc_now()
        host_image = image or vm.default_image
        # The janitor must be able to reap a VM after a client disconnects during provisioning.
        safety_expires_at = now + timedelta(seconds=self.settings.provisioning_grace_seconds)
        initial_expires_at = (
            max(expires_at, safety_expires_at)
            if expires_at is not ... and expires_at
            else safety_expires_at
        )
        host = Host(
            id=uid,
            service_account=service_account,
            env=env,
            secrets=secrets or {},
            name=name,
            provider=vm.name,
            image=host_image,
            instance_type=instance_type,
            disk_gb=disk_gb,
            status=HostStatus.PROVISIONING.value,
            created_at=now,
            updated_at=now,
            expires_at=initial_expires_at,
            pool_member=pool_member,
            lease_deadline=now + vm.max_lifetime if vm.max_lifetime else None,
        )
        if pool_member:
            expires_at = host.lease_expiry(
                ..., default=now + timedelta(hours=self.settings.pool_host_max_age_hours)
            )
        else:
            host.lease_expiry(expires_at, default=self._default_lease_expires_at())
        self.session.add(host)
        await self.session.commit()
        await self.session.refresh(host)

        await self.provision(str(host.id))
        await self.session.refresh(host)

        if host.status == HostStatus.ERROR.value:
            raise ProvisioningFailedError(host.last_error or "provisioning failed")

        # Use a separate session to preserve pool advisory locks. A concurrent renewal wins.
        try:
            expires_at = host.lease_expiry(expires_at, default=self._default_lease_expires_at())
        except HostLeaseError as exc:
            await self.mark_failed(host, exc)
            raise ProvisioningFailedError(str(exc)) from exc
        async with async_session_factory() as ttl_session:
            await ttl_session.execute(
                update(Host)
                .where(Host.id == host.id)
                .where(Host.expires_at == initial_expires_at)
                .values(expires_at=expires_at, updated_at=utc_now())
            )
            await ttl_session.commit()
        await self.session.refresh(host)
        return host

    async def _resolve_template_image(self, *, template_id: uuid.UUID, provider: str) -> str:
        result = await self.session.execute(
            select(Template).where(Template.id == template_id).where(Template.provider == provider)
        )
        template = result.scalar_one_or_none()

        if not template:
            raise UnknownTemplateError(
                f"template {template_id} not found for provider {provider!r}"
            )

        if template.status != TemplateStatus.AVAILABLE.value:
            detail = f"template {template.id} is {template.status}"
            if template.status == TemplateStatus.FAILED.value:
                detail = f"{detail}: {template.last_error}"
            raise TemplateNotAvailableError(detail)

        template.last_used_at = utc_now()
        return template.image

    async def _lookup_idempotency_key(self, key: str, service_account: str | None) -> Host | None:
        record = (
            await self.session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
        ).scalar_one_or_none()

        if not record:
            return

        host = (
            await self.session.get(Host, record.host_id) if record.expires_at > utc_now() else None
        )
        if host:
            if host.service_account != service_account:
                raise IdempotencyKeyConflictError(
                    f"idempotency key {key} belongs to another service account"
                )
            return host
        # A separate session avoids flushing pending host changes during key cleanup.
        async with async_session_factory() as gc_session:
            await gc_session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
            await gc_session.commit()
        return

    async def _record_idempotency_key(self, key: str, host: Host) -> bool:
        """Persist the key→host mapping; return False if a concurrent request won.

        Runs in a dedicated session so a losing-race UNIQUE violation can't roll
        back (and expire) the request's main session mid-response.
        """
        now = utc_now()
        async with async_session_factory() as record_session:
            record_session.add(
                IdempotencyKey(
                    key=key,
                    host_id=host.id,
                    created_at=now,
                    expires_at=now + timedelta(hours=self.settings.idempotency_key_ttl_hours),
                )
            )
            try:
                await record_session.commit()
            except IntegrityError:
                return False
        return True

    async def _release_idempotency_loser(self, host: Host) -> None:
        async with async_session_factory() as fix_session:
            fresh = await fix_session.get(Host, host.id)
            if not fresh:
                return
            now = utc_now()
            if fresh.claimed_at:
                fresh.claimed_at = None
                fresh.service_account = None
                pool_expiry = now + timedelta(hours=self.settings.pool_host_max_age_hours)
                fresh.expires_at = (
                    min(pool_expiry, fresh.lease_deadline) if fresh.lease_deadline else pool_expiry
                )
                fresh.updated_at = now
                logger.info(
                    "idempotency: returned pool host_id=%s to pool after lost race",
                    fresh.id,
                )
            else:
                fresh.expires_at = now
                fresh.updated_at = now
                logger.info(
                    "idempotency: marked host_id=%s for janitor reaping after lost race",
                    fresh.id,
                )
            await fix_session.commit()

    async def get_host(self, host_id: uuid.UUID) -> Host | None:
        return await self.session.get(Host, host_id)

    async def get_host_for_update(self, host_id: uuid.UUID) -> Host | None:
        result = await self.session.execute(
            select(Host).where(Host.id == host_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_hosts(self) -> list[Host]:
        result = await self.session.execute(select(Host).order_by(Host.created_at.desc()))
        return list(result.scalars())

    async def renew_host(self, host_id: uuid.UUID, *, expires_at: datetime | None = None) -> Host:
        host = await self.get_host_for_update(host_id)

        if not host:
            raise ResourceNotFoundError("host not found")

        if host.pool_member and not host.claimed_at:
            raise HostStateError("unclaimed pool host is managed by pool maintenance")

        if host.status not in RENEWABLE_STATUSES:
            raise HostStateError(f"cannot renew a host in status {host.status}")

        host.expires_at = host.lease_expiry(
            expires_at or ..., default=self._default_lease_expires_at()
        )
        host.updated_at = utc_now()
        await self.session.commit()
        await self.session.refresh(host)
        return host

    async def refresh_secret(self, host_id: uuid.UUID, service: str) -> None:
        host = await self.get_host(host_id)

        if not host:
            raise ResourceNotFoundError("host not found")

        if service not in host.secrets:
            raise ResourceNotFoundError("secret not found")

        if "value" in host.secrets[service]:
            raise SecretStaticError(f"secret {service} has a static value")

        await SecretsExchange.from_settings().refresh(host.id, service)

    async def delete_host(
        self,
        host_id: uuid.UUID,
        *,
        force: bool = False,
        pool_shed: bool = False,
        expired_only: bool = False,
    ) -> bool:
        """Delete the host; return False when a maintenance guard spared it."""
        host = await self.get_host_for_update(host_id)

        if not host:
            raise ResourceNotFoundError("host not found")

        if pool_shed and host.claimed_at:
            # A claim can occur after pool maintenance selects an excess host.
            return False

        if expired_only and (not host.expires_at or host.expires_at > utc_now()):
            # A renewal can occur after the janitor selects an expired host.
            return False

        if not force and host.status in DELETE_BLOCKED_STATUSES:
            raise HostStateError("host is still provisioning")

        if force or host.status in VM_BACKED_STATUSES:
            # An abandoned create can own a VM before its state reaches BOOTSTRAPPING.
            if host.tailscale_device_id and self.tailscale:
                # Commit device release so a failed VM deletion does not repeat it.
                await self.tailscale.release_device(host.tailscale_device_id)
                host.tailscale_device_id = None
                host.updated_at = utc_now()
                await self.session.commit()
            # Keep the row if secret deletion fails, so cleanup can be retried.
            vm = get_vm_provider(host.provider)
            await vm.secrets.delete_secrets(vm=host.name)
            try:
                await vm.delete_vm(host.name)
            except ProviderNotFoundError:
                logger.warning(
                    "host VM already absent at provider during teardown: "
                    "host_id=%s name=%s provider=%s",
                    host.id,
                    host.name,
                    host.provider,
                )
        await self.session.delete(host)
        await self.session.commit()
        return True

    async def provision(self, host_id: str) -> None:
        host = await self.get_host(uuid.UUID(host_id))

        if not host:
            raise ResourceNotFoundError("host not found")

        host.status = HostStatus.CREATING_NETWORK.value
        host.updated_at = utc_now()
        await self.session.commit()

        vm = get_vm_provider(host.provider)
        tailscale: Tailscale | None = None
        if vm.supports_tailnet:
            tailscale = self.tailscale

        environment = dict(host.env)
        setup_script: str | None = None
        if tailscale:
            try:
                join_credentials = await tailscale.issue_join_credentials(host_name=host.name)
            except NetworkError as exc:
                await self.mark_failed(host, exc)
                return
            environment.update(join_credentials.env)
            setup_script = _SANDBOX_BOOTSTRAP_SCRIPT

        try:
            environment.update(await self.put_secrets(host, vm))
            get_persist(environment)
        except (IssuerError, ProviderError, ValueError) as exc:
            await self.mark_failed(host, exc)
            return

        host.status = HostStatus.CREATING_VM.value
        host.updated_at = utc_now()
        await self.session.commit()

        gateway = GatewaySettings()
        if vm.gateway_process_class and not gateway.ssh_host:
            await self.mark_failed(
                host,
                ProvisioningFailedError(
                    f"provider {vm.name!r} requires the SSH gateway; set GATEWAY_SSH_HOST"
                ),
            )
            return
        try:
            vm_result = await vm.create_vm(
                name=host.name,
                image=host.image,
                env=environment,
                setup_script=setup_script,
                instance_type=host.instance_type,
                disk_gb=host.disk_gb,
            )
        except (ProviderCommandError, ProviderTransportError) as exc:
            await self.mark_failed(host, exc)
            return

        host.name = vm_result.name
        host.external_ssh_host = vm_result.ssh_host
        host.external_ssh_port = vm_result.ssh_port
        host.ssh_username = vm_result.ssh_username
        host.public_key = vm_result.public_key or ""
        if vm.gateway_process_class:
            host.external_ssh_host = gateway.ssh_host
            host.external_ssh_port = gateway.ssh_port
            host.ssh_username = host.name
        host.private_key = vm_result.private_key
        if tailscale:
            host.internal_ssh_host = tailscale.build_ssh_host(host.name)
        host.status = HostStatus.BOOTSTRAPPING.value
        host.updated_at = utc_now()
        await self.session.commit()

        if tailscale:
            try:
                device_id = await tailscale.wait_for_device(
                    host_name=host.name,
                    timeout=self.settings.device_discovery_timeout_seconds,
                )
            except (DeviceDiscoveryTimeoutError, NetworkError) as exc:
                await self.mark_failed(host, exc)
                return

            host.tailscale_device_id = device_id
            host.updated_at = utc_now()
            await self.session.commit()

        try:
            known_hosts_data = await self.scan_known_hosts(host)
        except RuntimeError as exc:
            await self.mark_failed(host, exc)
            return

        host.known_hosts = known_hosts_data.decode("utf-8")
        host.status = HostStatus.ACTIVE.value
        host.activated_at = utc_now()
        host.updated_at = utc_now()
        await self.session.commit()

    async def put_secrets(self, host: Host, vm: VMProvider) -> dict[str, str]:
        """Returns the environment the box needs."""
        environment: dict[str, str] = {}
        for name, entry in host.secrets.items():
            placeholder = Placeholder.mint(host.id, name)
            host.secrets[name] = {**entry, "placeholder_fingerprint": placeholder.fingerprint}
            value = await self.current_value(entry) if vm.secrets.needs_value else ""
            environment.update(
                await vm.secrets.put_secret(
                    vm=host.name,
                    service=catalog.service(name, entry),
                    placeholder=placeholder,
                    value=value,
                )
            )
        return environment

    @staticmethod
    async def current_value(entry: dict[str, Any]) -> str:
        if "value" in entry:
            return entry["value"]
        async with httpx.AsyncClient(timeout=10) as client:
            return (await Secret.fetch(entry["issuer"], client)).value

    async def mark_failed(self, host: Host, exc: Exception) -> None:
        logger.exception(
            "sandbox host failed: host_id=%s host_name=%s status=%s",
            host.id,
            host.name,
            host.status,
        )
        host.status = HostStatus.ERROR.value
        host.last_error = f"{type(exc).__name__}: {exc}"
        now = utc_now()
        host.expires_at = now
        host.updated_at = now
        await self.session.commit()

    async def scan_known_hosts(self, host: Host) -> bytes:
        # Tailscale SSH and public SSH can present different host keys.
        targets: list[tuple[str, int]] = []
        if host.internal_ssh_host:
            targets.append((host.internal_ssh_host, 22))
        if host.external_ssh_host:
            targets.append((host.external_ssh_host, host.external_ssh_port))
        timeout = get_vm_provider(host.provider).bootstrap_ssh_timeout_seconds
        deadline = time.monotonic() + timeout
        last_detail = "no attempt made"
        while True:
            scans = [await self._keyscan(ssh_host, ssh_port) for ssh_host, ssh_port in targets]
            collected = b"".join(stdout for stdout, _ in scans)
            if all(ssh_host.encode() in collected for ssh_host, _ in targets):
                return collected
            # Tailscale SSH can become ready after device discovery.
            last_detail = "; ".join(error for _, error in scans if error) or "empty output"
            if time.monotonic() >= deadline:
                raise RuntimeError(f"ssh-keyscan never returned host keys: {last_detail}")
            await asyncio.sleep(0.5)

    @staticmethod
    async def _keyscan(ssh_host: str, ssh_port: int) -> tuple[bytes, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                "ssh-keyscan",
                "-p",
                str(ssh_port),
                ssh_host,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise RuntimeError(f"could not run ssh-keyscan: {error}") from error
        stdout, stderr = await process.communicate()
        return stdout, stderr.decode().strip()
