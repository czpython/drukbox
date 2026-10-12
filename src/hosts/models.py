import uuid
from datetime import UTC, datetime
from enum import StrEnum
from types import EllipsisType

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, TypeDecorator, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy_encrypted_field import EncryptedJsonField, SecretsMapping
from uuid6 import uuid7

from core.database import Base
from hosts.exceptions import HostLeaseError

# JSONB is available on Postgres; SQLite keeps the local development database usable.
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
            # SQLite drops timezone offsets, so normalize before storage.
            return value.astimezone(UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value and not value.tzinfo:
            return value.replace(tzinfo=UTC)
        return value


UTCDateTime = _UTCDateTime()

_HOST_NAME_PREFIX = "sb-"
# Use UUIDv7's random suffix because its timestamp prefix is shared by concurrent creates.
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
    # The private key exists only on the create response and must never be stored.
    __allow_unmapped__ = True

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    env: Mapped[dict[str, str]] = mapped_column(_JSONType, default=dict)
    secrets: Mapped[SecretsMapping] = EncryptedJsonField()
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    service_account: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=HostStatus.PROVISIONING.value)
    provider: Mapped[str] = mapped_column(String(20), default="exe")
    image: Mapped[str] = mapped_column(Text)
    # Null sizing selects the provider default.
    instance_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    disk_gb: Mapped[int | None] = mapped_column(nullable=True, default=None)
    # The internal Tailscale SSH port is always 22.
    external_ssh_host: Mapped[str] = mapped_column(Text, default="")
    external_ssh_port: Mapped[int] = mapped_column(default=22)
    ssh_username: Mapped[str] = mapped_column(Text, default="")
    # Gateway callers authenticate against this public key.
    public_key: Mapped[str] = mapped_column(Text, default="")
    internal_ssh_host: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    known_hosts: Mapped[str] = mapped_column(Text, default="")
    tailscale_device_id: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    activated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
        default=None,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
        default=None,
    )
    lease_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
        default=None,
    )
    # An unclaimed caller-owned host must never be counted or removed as pool capacity.
    pool_member: Mapped[bool] = mapped_column(default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")
    private_key: str | None = None

    def lease_expiry(
        self, requested: datetime | None | EllipsisType, *, default: datetime
    ) -> datetime | None:
        expiry = default if requested is ... else requested
        if self.lease_deadline:
            if self.lease_deadline <= datetime.now(UTC):
                raise HostLeaseError("The provider lifetime has ended")
            if requested is ...:
                return min(default, self.lease_deadline)
            if not expiry or expiry > self.lease_deadline:
                raise HostLeaseError(
                    f"expires_at must be at or before {self.lease_deadline.isoformat()}"
                )
        return expiry

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
