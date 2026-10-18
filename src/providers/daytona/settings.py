from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import get_secrets_dir


class DaytonaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DAYTONA_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    api_key: str
    default_image: str
    target: str
    api_timeout: float = Field(default=150, gt=0)
    lifecycle_timeout_seconds: float = Field(default=180, gt=0)
    bootstrap_ssh_timeout_seconds: float = Field(default=120, gt=0)
