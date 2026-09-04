import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

RESERVED_HOST_ENV_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "DRUKBOX_AUTHORIZED_KEY",
        "DRUKBOX_ENV_KEYS",
        "DRUKBOX_SETUP_SCRIPT",
        "HTTPS_PROXY",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "TAILSCALE_AUTHKEY",
        "https_proxy",
        "no_proxy",
    }
)
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _expires_at_must_be_future_and_tz_aware(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must include a timezone offset")
    if expires_at <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return expires_at


class HostCreate(BaseModel):
    image: str | None = None
    template: uuid.UUID | None = Field(
        default=None,
        description="Template ID to fork from. Used only when the request has no image.",
    )
    env: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime | None = None
    provider: str | None = Field(
        default=None,
        description="VM provider to provision on. Omit to use the service default.",
    )
    instance_type: str | None = Field(
        default=None,
        description=(
            "Provider-native instance size, e.g. AWS `t3.xlarge` or Hetzner "
            "`cx33`. Omit to use the provider's configured default."
        ),
    )
    disk_gb: int | None = Field(
        default=None,
        ge=1,
        description="Root disk size in GB. Omit to use the provider's configured default.",
    )

    @field_validator("image", "instance_type")
    @classmethod
    def reject_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return value

    @field_validator("env")
    @classmethod
    def reject_reserved_env_keys(cls, env: dict[str, str]) -> dict[str, str]:
        # Enforce valid env-var-name keys before the reserved-name match: a key
        # like "TAILSCALE_AUTHKEY=x" or "DRUKBOX_AUTHORIZED_KEY " would slip past
        # the exact-name check, then a provider's env-file/shell export parses it
        # back into the reserved name it was meant to protect.
        if bad_keys := sorted(key for key in env if not _ENV_KEY_RE.fullmatch(key)):
            raise ValueError(
                f"env keys must be valid environment variable names: {', '.join(bad_keys)}"
            )
        reserved_keys = sorted(RESERVED_HOST_ENV_KEYS.intersection(env))
        if reserved_keys:
            raise ValueError(f"reserved env keys are not allowed: {', '.join(reserved_keys)}")
        return env

    _validate_expires_at = field_validator("expires_at")(_expires_at_must_be_future_and_tz_aware)


class HostRenew(BaseModel):
    # Omitted (or null) means "extend by LEASE_DEFAULT_TTL from now"; renewal
    # never makes a host permanent — that is a create-time choice.
    expires_at: datetime | None = None

    _validate_expires_at = field_validator("expires_at")(_expires_at_must_be_future_and_tz_aware)


class HostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: str
    provider: str
    image: str
    instance_type: str | None
    disk_gb: int | None
    external_ssh_host: str
    external_ssh_port: int
    ssh_username: str
    internal_ssh_host: str | None
    known_hosts: str
    tailscale_device_id: str | None
    private_key: str | None
    last_error: str
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None
    expires_at: datetime | None
