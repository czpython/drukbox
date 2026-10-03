from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import get_secrets_dir


class ExoscaleSettings(BaseSettings):
    """Exoscale provider configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="EXOSCALE_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    api_key: str = Field(
        description="Exoscale API key ID.",
    )
    api_secret: str = Field(
        description="Exoscale API secret used to sign requests.",
    )
    zone: str = Field(
        description="Exoscale zone for launches, e.g. ch-gva-2, de-fra-1.",
    )
    default_image: str = Field(
        default="Linux Ubuntu 24.04 LTS 64-bit",
        description="Template used when the caller doesn't pass one.",
    )
    instance_type: str = Field(
        default="standard.medium",
        description="Exoscale instance type when the caller doesn't pass one.",
    )
    disk_gb: int = Field(
        default=50,
        description="Root disk size in GB when the caller doesn't pass one.",
    )
    api_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for Exoscale API calls.",
    )
    bootstrap_ssh_timeout_seconds: float = Field(
        default=120.0,
        description="ssh-keyscan retry budget for a freshly-launched Exoscale instance.",
    )
    ssh_username: str = Field(
        default="ubuntu",
        description="In-VM user callers SSH as. Exoscale Ubuntu templates default to ubuntu.",
    )
