from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import CsvTuple, get_secrets_dir


class TailscaleSettings(BaseSettings):
    """Tailscale OAuth credentials and tailnet identity. Without them,
    drukbox has no tailnet."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TAILSCALE_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    tailnet: str = Field(
        default="",
        validation_alias="TAILSCALE_TAILNET",
        description="Tailnet DNS suffix used to build sandbox MagicDNS hostnames.",
    )
    auth_tags: CsvTuple = Field(
        default=(),
        validation_alias="TAILSCALE_AUTH_TAGS",
        description="Tags applied to Tailscale auth keys created for hosts (comma-separated).",
    )
    oauth_client_id: str = Field(
        default="",
        validation_alias="TAILSCALE_OAUTH_CLIENT_ID",
        description="OAuth client ID used for Tailscale API authentication.",
    )
    oauth_client_secret: str = Field(
        default="",
        validation_alias="TAILSCALE_OAUTH_CLIENT_SECRET",
        description="OAuth client secret used for Tailscale API authentication.",
    )
    api_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for Tailscale API calls.",
    )

    @model_validator(mode="after")
    def validate_credentials(self) -> Self:
        credentials = {
            "TAILSCALE_TAILNET": self.tailnet,
            "TAILSCALE_AUTH_TAGS": self.auth_tags,
            "TAILSCALE_OAUTH_CLIENT_ID": self.oauth_client_id,
            "TAILSCALE_OAUTH_CLIENT_SECRET": self.oauth_client_secret,
        }
        if any(credentials.values()) and not all(credentials.values()):
            missing = ", ".join(name for name, value in credentials.items() if not value)
            raise ValueError(f"Tailscale settings are incomplete. Set: {missing}")
        return self
