from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import get_secrets_dir


class BoatSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BOAT_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    api_token: str
    api_url: str = "https://boat.dev/api/v1"
    default_image: str = "default"
    instance_type: str = "default"
    api_timeout: float = Field(default=30, gt=0)
    provision_timeout: float = Field(default=300, gt=0)
    bootstrap_ssh_timeout_seconds: float = Field(default=120, gt=0)
