import asyncio
import logging
import pathlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, or_, select, update
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
    HostCapacityError,
    HostKeyScanError,
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
    ProviderError,
    ProviderNotFoundError,
    UnknownProviderError,
    UnsupportedSizingError,
)
from providers.registry import get_provider_names, get_vm_provider
from secrets_exchange.client import SecretsExchange
from secrets_exchange.secrets import IssuerError, MissingSecretError, Secret
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
        self.tailscale = tailscale or Tailscale.from_settings()

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
        expires_at: datetime | None = None,
        permanent: bool = False,
        idempotency_key: str | None = None,
        provider: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
    ) -> Host:
        if provider:
            registered = get_provider_names()
            if provider not in registered:
                available = ", ".join(sorted(registered))
                raise UnknownProviderError(f"unknown provider {provider!r}; available: {available}")

        if idempotency_key and (
            existing := await self._lookup_idempotency_key(idempotency_key, service_account)
        ):
            return existing

        host: Host | None = None
        # Pool members are warmed at the provider's defaults, so only an
        # uncustomized request can claim one.
        requested_provider = provider or self.settings.default_host_provider
        customized = env or secrets or image or template or instance_type or disk_gb
        if not customized and self.settings.get_pool_targets().get(requested_provider):
            host = await self._try_claim_pool_host(
                service_account=service_account,
                provider=requested_provider,
                expires_at=expires_at,
                permanent=permanent,
            )
        if not host:
            host = await self.create_host(
                service_account=service_account,
                env=env,
                secrets=secrets,
                image=image,
                template=template,
                expires_at=expires_at,
                permanent=permanent,
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
                raise HostStateError("idempotency race could not be resolved")
            return winner
        return host

    async def _try_claim_pool_host(
        self,
        *,
        service_account: str | None = None,
        provider: str,
        expires_at: datetime | None = None,
        permanent: bool = False,
    ) -> Host | None:
        # The claim's WHERE on claimed_at is the race guard: concurrent
        # claimants resolve to one winner per row on both dialects.
        now = utc_now()
        vm = get_vm_provider(provider)
        candidates = (
            select(Host)
            .where(Host.provider == provider)
            .where(Host.pool_member.is_(True))
            .where(Host.claimed_at.is_(None))
            .where(Host.status == HostStatus.ACTIVE.value)
            .where(or_(Host.expires_at.is_(None), Host.expires_at > now))
            .order_by(Host.created_at.asc())
            .limit(1)
        )
        if vm.max_lifetime and expires_at:
            # A warm host that the provider stops before the requested end is no match.
            candidates = candidates.where(Host.created_at >= expires_at - vm.max_lifetime)
        if candidate := (await self.session.execute(candidates)).scalar_one_or_none():
            # The claim replaces the warm-pool max-age TTL with the caller's lease.
            expires_at = None if permanent else expires_at or self._default_lease_expires_at()
            result = await self.session.execute(
                update(Host)
                .where(Host.id == candidate.id)
                .where(Host.claimed_at.is_(None))
                .values(
                    service_account=service_account,
                    claimed_at=now,
                    updated_at=now,
                    expires_at=vm.lease_end(candidate.created_at, expires_at),
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
        expires_at: datetime | None = None,
        permanent: bool = False,
        provider: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
        pool_member: bool = False,
    ) -> Host:
        # The pool maintainer calls this, not get_or_create_host, so a top-up
        # never claims its own members.
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
            found = await self.session.get(Template, template)
            if not found or found.provider != vm.name:
                raise UnknownTemplateError(
                    f"template {template} not found for provider {vm.name!r}"
                )
            if found.status != TemplateStatus.AVAILABLE.value:
                detail = f"template {found.id} is {found.status}"
                if found.status == TemplateStatus.FAILED.value:
                    detail = f"{detail}: {found.last_error}"
                raise TemplateNotAvailableError(detail)
            found.last_used_at = utc_now()
            image = found.image
        uid = uuid7()
        now = utc_now()
        # The safety TTL makes the janitor reap the row and VM if the client
        # disconnects mid-provision; the lease is stamped once the host is usable.
        safety_expires_at = now + timedelta(seconds=self.settings.provisioning_grace_seconds)
        initial_expires_at = max(expires_at, safety_expires_at) if expires_at else safety_expires_at
        host = Host(
            id=uid,
            service_account=service_account,
            env=env,
            secrets=secrets or {},
            name=Host.build_name(uid),
            provider=vm.name,
            image=image or vm.default_image,
            instance_type=instance_type,
            disk_gb=disk_gb,
            status=HostStatus.PROVISIONING.value,
            created_at=now,
            updated_at=now,
            expires_at=initial_expires_at,
            pool_member=pool_member,
        )
        if vm.name in self.settings.host_limits:
            # The lock serializes this provider's creates until the insert
            # commits, so two requests cannot both pass the count. SQLite has
            # no advisory locks; it serves single-process quickstarts.
            if self.session.get_bind().dialect.name == "postgresql":
                await self.session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtext(vm.name)))
                )
            live = (
                await self.session.execute(
                    select(func.count())
                    .select_from(Host)
                    .where(Host.provider == vm.name)
                    .where(Host.status != HostStatus.ERROR.value)
                    .where(or_(Host.expires_at.is_(None), Host.expires_at > now))
                )
            ).scalar_one()
            if live >= self.settings.host_limits[vm.name]:
                raise HostCapacityError(f"provider {vm.name!r} is at its host limit")
        self.session.add(host)
        await self.session.commit()
        await self.session.refresh(host)

        await self.provision(host)
        await self.session.refresh(host)

        if host.status == HostStatus.ERROR.value:
            raise ProvisioningFailedError(host.last_error)

        # A dedicated session, so self.session's transaction stays short for
        # callers that hold an advisory lock. The match on the in-flight value
        # spares a renewal that landed while the host was bootstrapping.
        expires_at = None if permanent else expires_at or self._default_lease_expires_at()
        async with async_session_factory() as ttl_session:
            await ttl_session.execute(
                update(Host)
                .where(Host.id == host.id)
                .where(Host.expires_at == initial_expires_at)
                .values(expires_at=vm.lease_end(host.created_at, expires_at), updated_at=utc_now())
            )
            await ttl_session.commit()
        await self.session.refresh(host)
        return host

    async def _lookup_idempotency_key(self, key: str, service_account: str | None) -> Host | None:
        if record := (
            await self.session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
        ).scalar_one_or_none():
            host = (
                await self.session.get(Host, record.host_id)
                if record.expires_at > utc_now()
                else None
            )
            if host:
                if host.service_account != service_account:
                    raise IdempotencyKeyConflictError(
                        f"idempotency key {key} belongs to another service account"
                    )
                return host
            # A stale key, expired or orphaned, goes in a dedicated session so
            # the caller's pending state on self.session is not flushed.
            async with async_session_factory() as gc_session:
                await gc_session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
                await gc_session.commit()

    async def _record_idempotency_key(self, key: str, host: Host) -> bool:
        """False when a concurrent request won. A dedicated session, so the lost
        race's UNIQUE violation cannot roll back the request's own session."""
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
        # A fresh loser is left to the janitor; delete_host refuses a host
        # that is still provisioning.
        async with async_session_factory() as fix_session:
            if fresh := await fix_session.get(Host, host.id):
                now = utc_now()
                if fresh.claimed_at:
                    fresh.claimed_at = None
                    fresh.service_account = None
                    fresh.expires_at = get_vm_provider(fresh.provider).lease_end(
                        fresh.created_at,
                        now + timedelta(hours=self.settings.pool_host_max_age_hours),
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

        host.expires_at = get_vm_provider(host.provider).lease_end(
            host.created_at, expires_at or self._default_lease_expires_at()
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
            # Claimed since the maintainer picked it as excess.
            return False

        if expired_only and (not host.expires_at or host.expires_at > utc_now()):
            # Renewed since the janitor picked it as expired.
            return False

        if not force and host.status in DELETE_BLOCKED_STATUSES:
            raise HostStateError("host is still provisioning")

        if force or host.status in VM_BACKED_STATUSES:
            # An abandoned provision stranded in CREATING_VM may already have a
            # VM; delete_vm no-ops without one.
            if host.tailscale_device_id and self.tailscale:
                # The cleared device_id commits before the VM goes, so a failed
                # delete_vm does not repeat the release. Without Tailscale
                # settings the auth-key TTL expires the device.
                await self.tailscale.release_device(host.tailscale_device_id)
                host.tailscale_device_id = None
                host.updated_at = utc_now()
                await self.session.commit()
            # Secrets go before the VM. A provider failure keeps the row for a retry.
            vm = get_vm_provider(host.provider)
            await vm.secrets.delete_secrets(vm=host.name)
            try:
                await vm.delete_vm(host.name)
            except ProviderNotFoundError:
                # Evicted by the provider, or a previous delete got this far.
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

    async def provision(self, host: Host) -> None:
        host.status = HostStatus.CREATING_NETWORK.value
        host.updated_at = utc_now()
        await self.session.commit()

        vm = get_vm_provider(host.provider)
        if vm.tailnet and not self.tailscale:
            await self.mark_failed(
                host,
                ProvisioningFailedError(
                    f"provider {vm.name!r} joins the tailnet; set TAILSCALE_TAILNET, "
                    "TAILSCALE_AUTH_TAGS, TAILSCALE_OAUTH_CLIENT_ID and "
                    "TAILSCALE_OAUTH_CLIENT_SECRET"
                ),
            )
            return
        tailscale = self.tailscale if vm.tailnet else None

        environment = dict(host.env)
        setup_script: str | None = None
        if tailscale:
            # The bootstrap script needs TAILSCALE_AUTHKEY, so only a tailnet
            # host runs it.
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
        except MissingSecretError as exc:
            await self.mark_failed(host, exc)
            raise
        except (IssuerError, ProviderError, ValueError) as exc:
            await self.mark_failed(host, exc)
            return

        host.status = HostStatus.CREATING_VM.value
        host.updated_at = utc_now()
        await self.session.commit()

        gateway = GatewaySettings()
        if vm.gateway_process_class and not gateway.ssh_host:
            # Without a gateway address the host's SSH coordinates would be dead.
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
        except ProviderError as exc:
            await self.mark_failed(host, exc)
            return

        host.name = vm_result.name
        host.external_ssh_host = vm_result.ssh_host
        host.external_ssh_port = vm_result.ssh_port
        host.ssh_username = vm_result.ssh_username
        host.public_key = vm_result.public_key or ""
        if vm.gateway_process_class:
            # The gateway is the SSH path for hosts of a gateway provider.
            # The username names the host; the per-host key is the credential.
            host.external_ssh_host = gateway.ssh_host
            host.external_ssh_port = gateway.ssh_port
            host.ssh_username = host.name
        # private_key has no column: this instance carries it into the POST
        # response, and a later GET never sees it.
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
        except HostKeyScanError as exc:
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
        # last_error reaches callers; the traceback stays in the log above.
        host.last_error = f"{type(exc).__name__}: {exc}"
        now = utc_now()
        # An errored host is dead weight (its VM may be half-created). Expire it
        # now so the janitor is the single owner of teardown.
        host.expires_at = now
        host.updated_at = now
        await self.session.commit()

    async def scan_known_hosts(self, host: Host) -> bytes:
        # The tailnet sshd and the provider's sshd present different keys, and
        # the tailnet one always listens on 22.
        targets: list[tuple[str, int]] = []
        if host.internal_ssh_host:
            targets.append((host.internal_ssh_host, 22))
        if host.external_ssh_host:
            targets.append((host.external_ssh_host, host.external_ssh_port))
        deadline = time.monotonic() + get_vm_provider(host.provider).bootstrap_ssh_timeout_seconds
        while True:
            collected = b""
            errors: list[str] = []
            for ssh_host, ssh_port in targets:
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
                    raise HostKeyScanError(f"could not run ssh-keyscan: {error}") from error
                stdout, stderr = await process.communicate()
                collected += stdout
                errors.append(stderr.decode().strip())
            if all(ssh_host.encode() in collected for ssh_host, _ in targets):
                return collected
            # Tailscaled-SSH lags device discovery; keyscan can connect but
            # read nothing during the gap.
            if time.monotonic() >= deadline:
                detail = "; ".join(filter(None, errors)) or "empty output"
                raise HostKeyScanError(f"ssh-keyscan never returned host keys: {detail}")
            await asyncio.sleep(0.5)
