import asyncio
import logging
import pathlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import EllipsisType

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from core.database import async_session_factory
from core.exceptions import ResourceNotFoundError
from core.settings import Settings, get_settings
from gateway.settings import GatewaySettings
from hosts.exceptions import HostStateError, ProvisioningFailedError
from hosts.models import Host, HostStatus, IdempotencyKey
from networking.tailscale import (
    DeviceDiscoveryTimeoutError,
    NetworkError,
    Tailscale,
)
from providers.exceptions import (
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
    UnknownProviderError,
    UnsupportedSizingError,
)
from providers.registry import get_provider_names, get_vm_provider
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
        # Construct Tailscale only when explicitly enabled. Settings'
        # model_validator guarantees the credentials are present whenever
        # tailscale_enabled is true. Tests can inject a mock Tailscale via
        # the kwarg regardless of the flag — useful for exercising the
        # tailnet path without real credentials.
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
        env: dict[str, str],
        image: str | None,
        template: uuid.UUID | None = None,
        expires_at: datetime | None | EllipsisType = ...,
        idempotency_key: str | None = None,
        provider: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
    ) -> Host:
        # ``...`` (omitted) means "default lease"; an explicit None is the
        # caller's deliberate opt-in to a permanent, never-reaped host. The
        # sentinel travels to the point where a lease is actually stamped
        # (pool claim, or the post-provision rewrite) so the in-flight row
        # keeps the short provisioning safety TTL.
        if provider:
            registered = get_provider_names()
            if provider not in registered:
                available = ", ".join(sorted(registered))
                raise UnknownProviderError(f"unknown provider {provider!r}; available: {available}")

        if idempotency_key:
            existing = await self._lookup_idempotency_key(idempotency_key)
            if existing:
                return existing

        host: Host | None = None
        # Warm hosts are provider-specific, so the claim is scoped to the
        # requested provider's pool. A request is pool-eligible only when it
        # does not customize the host: no image, template, env, or per-request
        # sizing — pool members are warmed at the provider's defaults.
        requested_provider = provider or self.settings.default_host_provider
        customized = env or image or template or instance_type or disk_gb
        if not customized and self.settings.get_pool_targets().get(requested_provider):
            host = await self._try_claim_pool_host(
                provider=requested_provider, expires_at=expires_at
            )
        if not host:
            host = await self.create_host(
                env=env,
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
            winner = await self._lookup_idempotency_key(idempotency_key)
            if not winner:
                raise HostStateError("idempotency race could not be resolved") from None
            return winner
        return host

    async def _try_claim_pool_host(
        self, *, provider: str, expires_at: datetime | None | EllipsisType
    ) -> Host | None:
        # Pick a candidate, then atomically claim it with UPDATE ... WHERE
        # claimed_at IS NULL ... RETURNING. The WHERE predicate is the actual
        # race guard — concurrent claimants resolve to a single winner per
        # row regardless of dialect (PG: MVCC + WHERE filter; SQLite: write
        # lock + WHERE filter). Losers return None and the caller falls
        # through to fresh provisioning.
        now = utc_now()
        candidate_id = (
            await self.session.execute(
                select(Host.id)
                .where(Host.provider == provider)
                .where(Host.pool_member.is_(True))
                .where(Host.claimed_at.is_(None))
                .where(Host.status == HostStatus.ACTIVE.value)
                .where(or_(Host.expires_at.is_(None), Host.expires_at > now))
                .order_by(Host.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not candidate_id:
            return

        if expires_at is ...:
            expires_at = self._default_lease_expires_at()
        # The claim replaces the warm-pool max-age TTL with the caller's lease:
        # a concrete window (explicit or the default), or None for a caller
        # who deliberately opted into a permanent host.
        result = await self.session.execute(
            update(Host)
            .where(Host.id == candidate_id)
            .where(Host.claimed_at.is_(None))
            .values(claimed_at=now, updated_at=now, expires_at=expires_at)
            .returning(Host)
        )
        host = result.scalar_one_or_none()
        await self.session.commit()
        if not host:
            # Lost the race to another claimant; let the caller fall through.
            return
        logger.info("pool: claimed host_id=%s name=%s", host.id, host.name)
        return host

    async def create_host(
        self,
        *,
        env: dict[str, str],
        image: str | None,
        template: uuid.UUID | None = None,
        expires_at: datetime | None | EllipsisType = ...,
        provider: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
        pool_member: bool = False,
    ) -> Host:
        # Always provisions a brand-new VM; the pool maintainer calls this
        # directly (with pool_member=True) so it never recursively claims its
        # own pool members.
        vm = get_vm_provider(provider)
        if instance_type and not vm.supports_instance_type:
            raise UnsupportedSizingError(
                f"provider {vm.name!r} does not support a per-request instance_type"
            )
        if disk_gb and not vm.supports_disk_gb:
            raise UnsupportedSizingError(
                f"provider {vm.name!r} does not support a per-request disk_gb"
            )
        if template and not image:
            image = await self._resolve_template_image(template_id=template, provider=vm.name)
        uid = uuid7()
        name = Host.build_name(uid)
        now = utc_now()
        host_image = image or vm.default_image
        # Safety TTL covers the strand window: if the client disconnects
        # mid-provision, this is what makes the janitor reap the row + VM.
        # Replaced with the caller's value after provisioning succeeds. A
        # default-lease create keeps just the safety TTL in flight — the
        # lease is stamped only once the host is usable.
        safety_expires_at = now + timedelta(seconds=self.settings.provisioning_grace_seconds)
        initial_expires_at = (
            max(expires_at, safety_expires_at)
            if isinstance(expires_at, datetime)
            else safety_expires_at
        )
        host = Host(
            id=uid,
            env=env,
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
        )
        self.session.add(host)
        await self.session.commit()
        await self.session.refresh(host)

        await self.provision(str(host.id))
        await self.session.refresh(host)

        if host.status == HostStatus.ERROR.value:
            raise ProvisioningFailedError(host.last_error or "provisioning failed")

        # Provisioning won: replace the safety TTL with the caller's intent
        # in a dedicated session so we don't extend ``self.session``'s
        # transaction (which can perturb advisory-lock-bearing callers like
        # the pool maintainer). Guarded on the in-flight value: a renewal
        # that landed while the host was bootstrapping is newer intent and
        # must not be clobbered.
        if expires_at is ...:
            expires_at = self._default_lease_expires_at()
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

    async def _lookup_idempotency_key(self, key: str) -> Host | None:
        record = (
            await self.session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
        ).scalar_one_or_none()

        if not record:
            return

        if record.expires_at > utc_now():
            host = await self.session.get(Host, record.host_id)
            if host:
                return host
        # Stale: expired, or the host vanished without the FK cascade firing.
        # GC in a dedicated session so we don't autoflush the caller's pending
        # state on `self.session`.
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
        # Two shapes of loser: claimed pool host → return to pool with a
        # fresh max-age TTL; freshly-created host → mark expired so the
        # janitor reaps it (delete_host refuses PROVISIONING).
        async with async_session_factory() as fix_session:
            fresh = await fix_session.get(Host, host.id)
            if not fresh:
                return
            now = utc_now()
            if fresh.claimed_at:
                fresh.claimed_at = None
                fresh.expires_at = now + timedelta(hours=self.settings.pool_host_max_age_hours)
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

        host.expires_at = expires_at or self._default_lease_expires_at()
        host.updated_at = utc_now()
        await self.session.commit()
        await self.session.refresh(host)
        return host

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
            # A caller claimed this host between the maintainer selecting it as
            # excess and this locked read — leave it for its owner, don't reap it.
            return False

        if expired_only and (not host.expires_at or host.expires_at > utc_now()):
            # The owner renewed this host between the janitor selecting it as
            # expired and this locked read — the lease is live again, spare it.
            return False

        if not force and host.status in DELETE_BLOCKED_STATUSES:
            raise HostStateError("host is still provisioning")

        if force or host.status in VM_BACKED_STATUSES:
            # force is the janitor reaping an abandoned provision: attempt
            # teardown even from an early state, since a row stranded in
            # CREATING_VM may already have a VM (delete_vm no-ops if it doesn't).
            if host.tailscale_device_id and self.tailscale:
                # Clear and commit the device_id before deleting the VM:
                # a later delete_vm transport error must not retry the
                # already-completed release. Hosts provisioned under
                # Tailscale but reaped after the operator turned it off
                # fall through and let the auth-key TTL expire the device.
                await self.tailscale.release_device(host.tailscale_device_id)
                host.tailscale_device_id = None
                host.updated_at = utc_now()
                await self.session.commit()
            try:
                await get_vm_provider(host.provider).delete_vm(host.name)
            except ProviderNotFoundError:
                # VM already absent at the provider — exe.dev may have evicted
                # it, or a previous delete partially succeeded. Treat as done
                # so we can clean up the DB row, but log so unexpected
                # evictions are visible.
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

        tailscale: Tailscale | None = None
        if get_vm_provider(host.provider).supports_tailnet:
            tailscale = self.tailscale

        join_env: dict[str, str] = {}
        setup_script: str | None = None
        if tailscale:
            # The bootstrap script hard-requires TAILSCALE_AUTHKEY; only
            # deliver it (and mint a key) when Tailscale is in play.
            try:
                join_credentials = await tailscale.issue_join_credentials(host_name=host.name)
            except NetworkError as exc:
                await self.mark_failed(host, exc)
                return
            join_env = dict(join_credentials.env)
            setup_script = _SANDBOX_BOOTSTRAP_SCRIPT

        environment = {**host.env, **join_env}

        host.status = HostStatus.CREATING_VM.value
        host.updated_at = utc_now()
        await self.session.commit()

        vm = get_vm_provider(host.provider)
        gateway = GatewaySettings()
        if vm.gateway_process_class and not gateway.ssh_host:
            # A gateway-provider host is reachable only through the gateway;
            # provisioning one without an address would hand out dead
            # coordinates.
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
            # The gateway is the SSH path for hosts of a gateway provider.
            # The username names the host; the per-host key is the credential.
            host.external_ssh_host = gateway.ssh_host
            host.external_ssh_port = gateway.ssh_port
            host.ssh_username = host.name
        # Stamp the per-VM key onto this instance so the POST response
        # carries it. There's no column behind `private_key`, so a later
        # GET that loads a fresh row sees the class default (None) and
        # never echoes the key back.
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

    async def mark_failed(self, host: Host, exc: Exception) -> None:
        logger.exception(
            "sandbox host failed: host_id=%s host_name=%s status=%s",
            host.id,
            host.name,
            host.status,
        )
        host.status = HostStatus.ERROR.value
        # Client-safe summary, not the raw traceback: last_error is echoed back
        # to callers, while the full traceback stays in the log above.
        host.last_error = f"{type(exc).__name__}: {exc}"
        now = utc_now()
        # An errored host is dead weight (its VM may be half-created). Expire it
        # now so the janitor is the single owner of teardown; the POST caller
        # already got last_error in the 502.
        host.expires_at = now
        host.updated_at = now
        await self.session.commit()

    async def scan_known_hosts(self, host: Host) -> bytes:
        # Scan every reachable address. tailscaled-SSH (internal) and the
        # provider's edge sshd (external) present different host keys, so
        # callers picking either path need both entries to verify. Each address
        # carries its own port: the internal path is always 22 by Tailscale
        # convention, while the external sshd may be remapped (e.g. a published
        # container port), so they're scanned separately.
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
            # Tailscaled-SSH lags device discovery; keyscan can connect but
            # read nothing during the gap. Retry within the budget.
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
            # ssh-keyscan missing from the image (or otherwise unspawnable) raises
            # here; translate to the RuntimeError provision() routes through
            # mark_failed, so it surfaces as a provisioning failure, not a 500.
            raise RuntimeError(f"could not run ssh-keyscan: {error}") from error
        stdout, stderr = await process.communicate()
        return stdout, stderr.decode().strip()
