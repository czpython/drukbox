from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return value


CsvTuple = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        validation_alias="DATABASE_URL",
        description="Async SQLAlchemy database URL for drukbox state.",
    )
    service_tokens: CsvTuple = Field(
        min_length=1,
        validation_alias="SERVICE_TOKENS",
        description="Bearer tokens accepted from trusted service clients (comma-separated).",
    )
    default_host_provider: str = Field(
        default="exe",
        validation_alias="DEFAULT_HOST_PROVIDER",
        description="Name of the VM provider used when callers don't specify one.",
    )
    default_network_provider: str = Field(
        default="tailscale",
        validation_alias="DEFAULT_NETWORK_PROVIDER",
        description="Name of the network provider used when callers don't specify one.",
    )

    service_label: str = Field(
        default="drukbox",
        validation_alias="SERVICE_LABEL",
        description="Identifier stamped onto provider resources (e.g. VM tags).",
    )

    tailscale_enabled: bool = Field(
        default=False,
        validation_alias="TAILSCALE_ENABLED",
        description="Enable Tailscale-based reachability for provisioned hosts.",
    )
    device_discovery_timeout_seconds: float = Field(
        default=180.0,
        gt=0,
        validation_alias="DEVICE_DISCOVERY_TIMEOUT_SECONDS",
        description="How long to wait for a sandbox VM to appear in Tailscale.",
    )
    idempotency_key_ttl_hours: int = Field(
        default=24,
        gt=0,
        validation_alias="IDEMPOTENCY_KEY_TTL_HOURS",
        description="How long an Idempotency-Key stays in force.",
    )
    provisioning_grace_seconds: int = Field(
        default=600,
        gt=0,
        validation_alias="PROVISIONING_GRACE_SECONDS",
        description="Safety TTL on the host row while provisioning is in flight.",
    )
    template_build_timeout: int = Field(
        default=3600,
        gt=0,
        validation_alias="TEMPLATE_BUILD_TIMEOUT",
        description=(
            "Max age in seconds of an unfinished template build before the janitor marks it failed."
        ),
    )
    template_failed_retention: int = Field(
        default=86400,
        gt=0,
        validation_alias="TEMPLATE_FAILED_RETENTION",
        description=(
            "Seconds to keep failed templates for diagnosis before the janitor deletes them."
        ),
    )
    template_unused_ttl: int = Field(
        default=1209600,
        gt=0,
        validation_alias="TEMPLATE_UNUSED_TTL",
        description="Seconds to keep an available template after its last use.",
    )
    lease_default_ttl: int = Field(
        default=86400,
        gt=0,
        validation_alias="LEASE_DEFAULT_TTL",
        description="Lease TTL in seconds for hosts created without an explicit expires_at.",
    )
    pool_sizes: dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict,
        validation_alias="POOL_SIZES",
        description=(
            'Pre-warmed host targets per provider, as JSON (e.g. {"exe": 2, "hetzner": 1}). '
            "Overrides POOL_SIZE for the providers it names."
        ),
    )
    pool_size: int = Field(
        default=0,
        ge=0,
        validation_alias="POOL_SIZE",
        description=(
            "Number of pre-warmed hosts to keep ready for the default provider. "
            "0 disables its pool. A POOL_SIZES entry for that provider wins."
        ),
    )
    pool_host_max_age_hours: int = Field(
        default=4,
        gt=0,
        validation_alias="POOL_HOST_MAX_AGE_HOURS",
        description="Max age in hours before the janitor reaps an unclaimed pool host.",
    )
    pool_max_creates_per_tick: int = Field(
        default=2,
        ge=0,
        validation_alias="POOL_MAX_CREATES_PER_TICK",
        description="Upper bound on pool-maintainer provisions per tick, across all providers.",
    )

    def get_pool_targets(self) -> dict[str, int]:
        # POOL_SIZE seeds the default provider's target and POOL_SIZES
        # overrides per provider; providers at zero drop out entirely.
        targets = {self.default_host_provider: self.pool_size, **self.pool_sizes}
        return {provider: target for provider, target in targets.items() if target > 0}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
