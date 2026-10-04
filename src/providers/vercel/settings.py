from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import get_secrets_dir


class VercelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="VERCEL_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=get_secrets_dir(),
    )

    token: str
    team_id: str
    project_id: str
    default_image: str
    vcpus: int = Field(default=2, ge=1, le=32)
    session_timeout_seconds: int = Field(default=2700, ge=600, le=86400)
    api_timeout: float = Field(default=150, gt=0)
    bootstrap_ssh_timeout_seconds: float = Field(default=120, gt=0)
