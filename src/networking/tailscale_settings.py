from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import CsvTuple, get_secrets_dir


class TailscaleSettings(BaseSettings):
    """Tailscale OAuth credentials and tailnet identity."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TAILSCALE_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    tailnet: str = Field(
        validation_alias="TAILSCALE_TAILNET",
        description="Tailnet DNS suffix used to build sandbox MagicDNS hostnames.",
    )
    auth_tags: CsvTuple = Field(
        validation_alias="TAILSCALE_AUTH_TAGS",
        description="Tags applied to Tailscale auth keys created for hosts (comma-separated).",
    )
    oauth_client_id: str = Field(
        validation_alias="TAILSCALE_OAUTH_CLIENT_ID",
        description="OAuth client ID used for Tailscale API authentication.",
    )
    oauth_client_secret: str = Field(
        validation_alias="TAILSCALE_OAUTH_CLIENT_SECRET",
        description="OAuth client secret used for Tailscale API authentication.",
    )
    api_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for Tailscale API calls.",
    )
