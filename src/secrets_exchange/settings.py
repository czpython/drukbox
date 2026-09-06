from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretsExchangeSettings(BaseSettings):
    """Secrets exchange process configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SECRETS_EXCHANGE_",
        extra="ignore",
    )

    bind_host: str = Field(
        default="127.0.0.1",
        description=(
            "Interface the exchange process binds. Bind it where only the proxy can reach it."
        ),
    )
    port: int = Field(default=8781, description="Port the exchange process listens on.")
