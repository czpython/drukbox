from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import get_secrets_dir


class CloudflareSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CLOUDFLARE_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    worker_url: str
    worker_token: str
    default_image: str = "base"
    instance_type: str = "standard-1"
    api_timeout: float = Field(default=150, gt=0)
    bootstrap_ssh_timeout_seconds: float = Field(default=120, gt=0)
