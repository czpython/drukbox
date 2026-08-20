import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, TypeDecorator, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from core.database import Base

# Use JSONB on Postgres (indexable, binary storage); fall back to JSON
# (TEXT-backed) on SQLite and other dialects so the OSS quickstart works
# without Postgres.
_JSONType = JSON().with_variant(JSONB(), "postgresql")


class _UTCDateTime(TypeDecorator[datetime]):
    """DateTime that always reads back as a UTC-aware datetime.

    SQLite's DateTime(timezone=True) round-trips as a tz-naive datetime,
    which breaks any caller that does arithmetic with tz-aware datetimes
    (the rest of the codebase). This decorator normalizes aware values to UTC
    on write and re-attaches UTC on read, so a non-UTC offset can't be
    reinterpreted as UTC and callers see the same shape regardless of backend.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value and value.tzinfo:
            # SQLite drops the offset, so store the equivalent UTC instant —
            # otherwise 00:00-08:00 reads back as 00:00Z, not 08:00Z.
            return value.astimezone(UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


_DateTimeUTC = _UTCDateTime()

_HOST_NAME_PREFIX = "sb-"
# 48 bits of UUIDv7 entropy for a short readable name. UUIDv7's leading 48
# bits are the millisecond timestamp — concurrent creates in the same ms
# share an identical leading-hex prefix and collided on the unique index.
# Slice from the trailing random segment (rand_b, bits 64..125) so names
# are derived from actual entropy, not a clock reading.
_HOST_NAME_UID_CHARS = 12


class HostStatus(StrEnum):
    PROVISIONING = "provisioning"
    CREATING_NETWORK = "creating_network"
    CREATING_VM = "creating_vm"
    BOOTSTRAPPING = "bootstrapping"  # VM created; waiting for Tailscale discovery + ssh-keyscan.
    ACTIVE = "active"
    ERROR = "error"


class Host(Base):
    __tablename__ = "hosts"
    # Allow non-Mapped[] annotations on this class (we use it for
    # `private_key`, a transient per-instance attribute that must never
    # be persisted). Without this flag SQLAlchemy 2.0's annotated
    # declarative mapper rejects plain annotations.
    __allow_unmapped__ = True

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    env: Mapped[dict[str, str]] = mapped_column(_JSONType, default=dict)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=HostStatus.PROVISIONING.value)
    provider: Mapped[str] = mapped_column(String(20), default="exe")
    image: Mapped[str] = mapped_column(Text)
    # Per-request sizing, provider-native values (EC2 instance type, Hetzner
    # server type). NULL means the provider's configured default size.
    instance_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    disk_gb: Mapped[int | None] = mapped_column(nullable=True, default=None)
    # Reachable SSH addresses. Both populated when Tailscale is enabled
    # (internal = MagicDNS name, external = provider-given address); only
    # external_ssh_host is populated when Tailscale is disabled. The
    # internal path is always reached on port 22 by Tailscale convention,
    # so no internal_ssh_port column.
    external_ssh_host: Mapped[str] = mapped_column(Text, default="")
    external_ssh_port: Mapped[int] = mapped_column(default=22)
    ssh_username: Mapped[str] = mapped_column(Text, default="")
    # The public half of the per-host keypair. The gateway authenticates
    # callers against it. The private half is returned once and never stored.
    public_key: Mapped[str] = mapped_column(Text, default="")
    internal_ssh_host: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    known_hosts: Mapped[str] = mapped_column(Text, default="")
    tailscale_device_id: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(_DateTimeUTC)
    updated_at: Mapped[datetime] = mapped_column(_DateTimeUTC)
    activated_at: Mapped[datetime | None] = mapped_column(
        _DateTimeUTC,
        nullable=True,
        default=None,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        _DateTimeUTC,
        nullable=True,
        default=None,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        _DateTimeUTC,
        nullable=True,
        default=None,
    )
    # True only for hosts the pool maintainer warmed. Demand-provisioned hosts
    # are False so pool claim/count/shed never hand out or delete a caller-owned
    # sandbox — both kinds start with claimed_at NULL, so claimed_at alone can't
    # tell them apart.
    pool_member: Mapped[bool] = mapped_column(default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")
    # Non-persisted, transient per-instance attribute. provision() assigns
    # the freshly-minted private key here so HostOut returns it exactly
    # once at create time; a subsequent GET reads a row from disk where
    # this attribute falls back to None. `__allow_unmapped__` above lets
    # SQLAlchemy treat the plain annotation as a class attribute instead
    # of a missing column.
    private_key: str | None = None

    def __str__(self) -> str:
        return f"{self.provider}:{self.name}"

    @classmethod
    def build_name(cls, host_id: uuid.UUID) -> str:
        return f"{_HOST_NAME_PREFIX}{host_id.hex[-_HOST_NAME_UID_CHARS:]}"


class IdempotencyKey(Base):
    """Maps caller-supplied Idempotency-Key headers to the host they created.

    Cascade-delete on the host means a deleted host invalidates its key — a
    retry with the same key after the host has been torn down starts fresh.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(_DateTimeUTC)
    expires_at: Mapped[datetime] = mapped_column(_DateTimeUTC, index=True)
