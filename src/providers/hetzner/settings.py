from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import get_secrets_dir


class HetznerSettings(BaseSettings):
    """Hetzner Cloud provider configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="HETZNER_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    api_token: str = Field(
        description="Bearer token for the Hetzner Cloud API.",
    )
    default_image: str = Field(
        default="ubuntu-24.04",
        description="Image when the caller doesn't pass one (Hetzner image name or id).",
    )
    location: str = Field(
        description="Hetzner location for launches, e.g. nbg1, fsn1, hel1, ash.",
    )
    server_type: str = Field(
        default="cx23",
        description="Hetzner server type, e.g. cx23, cx33. Avoid deprecated Gen2 types (cx22).",
    )
    api_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for Hetzner API calls.",
    )
    bootstrap_ssh_timeout_seconds: float = Field(
        default=120.0,
        description="ssh-keyscan retry budget for a freshly-launched Hetzner server.",
    )
    ssh_username: str = Field(
        default="root",
        description="In-VM user callers SSH as. Hetzner cloud images default to root.",
    )
