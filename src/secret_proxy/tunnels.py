import asyncio
import fcntl
import logging
import os
import stat
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import asyncssh
from sqlalchemy import select, update

from core.database import async_session_factory
from hosts.models import Host, HostStatus
from providers.capabilities import ReverseTunnelCapability
from providers.registry import get_vm_provider
from secret_proxy import TUNNEL_IDENTITY_PREFIX
from secret_proxy.exceptions import ReverseTunnelError
from secret_proxy.settings import SecretProxySettings

logger = logging.getLogger(__name__)

TunnelDropped = Callable[["ReverseTunnel"], Awaitable[None]]


@lru_cache
def load_reverse_tunnel_key(path: Path) -> asyncssh.SSHKey:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "r+b") as key_file:
            fcntl.flock(key_file, fcntl.LOCK_EX)
            mode = stat.S_IMODE(os.fstat(key_file.fileno()).st_mode)
            if mode & 0o077:
                raise ReverseTunnelError("reverse tunnel key must not be group or world accessible")
            private_key = key_file.read()
            if not private_key:
                private_key = asyncssh.generate_private_key("ssh-ed25519").export_private_key(
                    "openssh"
                )
                key_file.write(private_key)
                key_file.flush()
                os.fsync(key_file.fileno())
    except OSError as exc:
        raise ReverseTunnelError("reverse tunnel key could not be loaded") from exc

    try:
        return asyncssh.import_private_key(private_key)
    except asyncssh.KeyImportError as exc:
        raise ReverseTunnelError("reverse tunnel key could not be loaded") from exc


class ReverseTunnel:
    def __init__(
        self,
        *,
        host_id: uuid.UUID,
        connection: asyncssh.SSHClientConnection,
        listener: asyncssh.SSHListener,
        dropped: TunnelDropped,
    ) -> None:
        self.host_id = host_id
        self.connection = connection
        self.listener = listener
        self._dropped = dropped
        self._closing = False
        self._monitor = asyncio.create_task(self._monitor_transport())

    @classmethod
    async def open(
        cls,
        *,
        host_id: uuid.UUID,
        host_name: str,
        ssh_host: str,
        ssh_port: int,
        ssh_username: str,
        known_hosts: str,
        client_key: asyncssh.SSHKey | None,
        settings: SecretProxySettings,
        dropped: TunnelDropped,
    ) -> "ReverseTunnel":
        client_keys = [client_key] if client_key else []
        try:
            connection = await asyncssh.connect(
                ssh_host,
                port=ssh_port,
                username=ssh_username,
                known_hosts=known_hosts.encode(),
                client_keys=client_keys,
                connect_timeout=settings.tunnel_connect_timeout_seconds,
                keepalive_interval=settings.tunnel_keepalive_interval_seconds,
                keepalive_count_max=settings.tunnel_keepalive_count_max,
            )
            try:

                async def forward_to_proxy(
                    _origin_host: str,
                    _origin_port: int,
                ) -> asyncssh.SSHForwarder:
                    forwarder = await connection.forward_connection(
                        settings.tunnel_target_host,
                        settings.bind_port,
                    )
                    forwarder.data_received(
                        TUNNEL_IDENTITY_PREFIX + host_name.encode("ascii") + b"\r\n"
                    )
                    return forwarder

                listener = await connection.create_server(
                    forward_to_proxy,
                    "127.0.0.1",
                    settings.tunnel_box_port,
                )
            except BaseException:
                connection.close()
                await connection.wait_closed()
                raise
        except (OSError, asyncssh.Error) as exc:
            raise ReverseTunnelError("reverse tunnel could not connect") from exc
        return cls(
            host_id=host_id,
            connection=connection,
            listener=listener,
            dropped=dropped,
        )

    async def aclose(self) -> None:
        self._closing = True
        self.listener.close()
        self.connection.close()
        await self.listener.wait_closed()
        await self.connection.wait_closed()
        await self._monitor

    async def _monitor_transport(self) -> None:
        connection_closed = asyncio.create_task(self.connection.wait_closed())
        listener_closed = asyncio.create_task(self.listener.wait_closed())
        _, pending = await asyncio.wait(
            {connection_closed, listener_closed},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if not self._closing:
            self.listener.close()
            self.connection.close()
            await self.listener.wait_closed()
            await self.connection.wait_closed()
            await self._dropped(self)


class ReverseTunnelManager:
    def __init__(self, settings: SecretProxySettings | None = None) -> None:
        self.settings = settings or SecretProxySettings()
        owner_path = Path(f"{self.settings.expanded_tunnel_key_path}.lock")
        self._owner_descriptor = -1
        try:
            owner_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._owner_descriptor = os.open(
                owner_path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            fcntl.flock(
                self._owner_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            if self._owner_descriptor >= 0:
                os.close(self._owner_descriptor)
            raise ReverseTunnelError(
                "another API process already owns the reverse tunnels"
            ) from exc
        except OSError as exc:
            if self._owner_descriptor >= 0:
                os.close(self._owner_descriptor)
            raise ReverseTunnelError("reverse tunnel ownership lock could not be acquired") from exc
        try:
            self._client_key = load_reverse_tunnel_key(self.settings.expanded_tunnel_key_path)
        except BaseException:
            os.close(self._owner_descriptor)
            raise
        self.public_key = self._client_key.export_public_key("openssh").decode().strip()
        self._tunnels: dict[uuid.UUID, ReverseTunnel] = {}
        self._opening: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        if self._reconcile_task:
            raise ReverseTunnelError("reverse tunnel manager is already started")
        if self._closing:
            raise ReverseTunnelError("reverse tunnel manager is closed")
        await self._reconcile()
        self._reconcile_task = asyncio.create_task(self._reconcile_forever())

    async def ensure(self, host: Host, *, timeout_seconds: float) -> None:
        async with self._lock:
            if self._closing:
                raise ReverseTunnelError("reverse tunnel manager is closed")
            existing = self._tunnels.get(host.id)
            if existing:
                if existing.connection.is_closed():
                    raise ReverseTunnelError("reverse tunnel is disconnected")
                return
            opening = self._opening.get(host.id)
            if not opening:
                opening = asyncio.create_task(self._open(host, timeout_seconds=timeout_seconds))
                self._opening[host.id] = opening

        try:
            await opening
        finally:
            async with self._lock:
                if self._opening.get(host.id) is opening:
                    del self._opening[host.id]

    async def close(self, host_id: uuid.UUID) -> None:
        async with self._lock:
            opening = self._opening.pop(host_id, None)
            tunnel = self._tunnels.pop(host_id, None)
        if opening:
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
        if tunnel:
            await tunnel.aclose()

    async def aclose(self) -> None:
        self._closing = True
        if self._reconcile_task:
            self._reconcile_task.cancel()
            await asyncio.gather(self._reconcile_task, return_exceptions=True)
            self._reconcile_task = None
        async with self._lock:
            opening = list(self._opening.values())
            self._opening.clear()
            tunnels = list(self._tunnels.values())
            self._tunnels.clear()
        for task in opening:
            task.cancel()
        await asyncio.gather(*opening, return_exceptions=True)
        try:
            await asyncio.gather(*(tunnel.aclose() for tunnel in tunnels))
        finally:
            if self._owner_descriptor >= 0:
                os.close(self._owner_descriptor)
                self._owner_descriptor = -1

    async def _open(self, host: Host, *, timeout_seconds: float) -> None:
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    try:
                        tunnel = await ReverseTunnel.open(
                            host_id=host.id,
                            host_name=host.name,
                            ssh_host=host.internal_ssh_host or host.external_ssh_host,
                            ssh_port=22 if host.internal_ssh_host else host.external_ssh_port,
                            ssh_username=host.ssh_username,
                            known_hosts=host.known_hosts,
                            client_key=None if host.internal_ssh_host else self._client_key,
                            settings=self.settings,
                            dropped=self._tunnel_dropped,
                        )
                    except ReverseTunnelError:
                        await asyncio.sleep(0.5)
                        continue
                    try:
                        async with self._lock:
                            if self._closing:
                                raise ReverseTunnelError("reverse tunnel manager is closed")
                            self._tunnels[host.id] = tunnel
                    except BaseException:
                        await tunnel.aclose()
                        raise
                    logger.info(
                        "reverse tunnel open: host_id=%s host_name=%s",
                        host.id,
                        host.name,
                    )
                    return
        except TimeoutError as exc:
            raise ReverseTunnelError("reverse tunnel could not connect before the timeout") from exc

    async def _tunnel_dropped(self, tunnel: ReverseTunnel) -> None:
        async with self._lock:
            current = self._tunnels.get(tunnel.host_id)
            if current is tunnel:
                del self._tunnels[tunnel.host_id]
        if not self._closing and current is tunnel:
            logger.error("reverse tunnel dropped: host_id=%s", tunnel.host_id)
            try:
                await self._mark_failed(
                    tunnel.host_id,
                    "ReverseTunnelError: reverse tunnel disconnected; create a replacement host",
                )
            except Exception:
                logger.exception(
                    "reverse tunnel failure could not be recorded: host_id=%s",
                    tunnel.host_id,
                )

    async def _reconcile_forever(self) -> None:
        while True:
            try:
                await self._reconcile()
            except Exception:
                logger.exception("reverse tunnel reconciliation failed")
            await asyncio.sleep(self.settings.tunnel_reconcile_interval_seconds)

    async def _reconcile(self) -> None:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Host).where(
                    Host.status.in_((HostStatus.BOOTSTRAPPING.value, HostStatus.ACTIVE.value))
                )
            )
            hosts = list(result.scalars())

        tunnel_hosts = [
            host
            for host in hosts
            if isinstance(get_vm_provider(host.provider), ReverseTunnelCapability)
        ]
        desired = {host.id for host in tunnel_hosts}
        async with self._lock:
            stale = set(self._tunnels) - desired
        await asyncio.gather(*(self.close(host_id) for host_id in stale))
        await asyncio.gather(
            *(
                self._restore(host)
                for host in tunnel_hosts
                if host.status == HostStatus.ACTIVE.value
            )
        )

    async def _restore(self, host: Host) -> None:
        try:
            await self.ensure(
                host,
                timeout_seconds=self.settings.tunnel_connect_timeout_seconds,
            )
        except ReverseTunnelError:
            logger.exception("reverse tunnel restore failed: host_id=%s", host.id)
            await self._mark_failed(
                host.id,
                "ReverseTunnelError: reverse tunnel could not be restored; "
                "create a replacement host",
            )

    @staticmethod
    async def _mark_failed(host_id: uuid.UUID, detail: str) -> None:
        now = datetime.now(UTC)
        async with async_session_factory() as session:
            await session.execute(
                update(Host)
                .where(Host.id == host_id)
                .where(Host.status.in_((HostStatus.BOOTSTRAPPING.value, HostStatus.ACTIVE.value)))
                .values(
                    status=HostStatus.ERROR.value,
                    last_error=detail,
                    expires_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
