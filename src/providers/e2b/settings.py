from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import get_secrets_dir


class E2BSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="E2B_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    api_key: str
    default_image: str
    session_timeout_seconds: int = Field(default=3600, ge=600, le=86400)
    api_timeout: float = Field(default=150, gt=0)
    bootstrap_ssh_timeout_seconds: float = Field(default=120, gt=0)
