from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """SSH gateway configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GATEWAY_",
        extra="ignore",
    )

    ssh_host: str = Field(
        default="",
        description=(
            "Address of the gateway. Gateway-provider hosts advertise it; "
            "the service refuses to provision them without it."
        ),
    )
    ssh_port: int = Field(
        default=2222,
        description="Port the gateway listens on and advertises.",
    )
    bind_host: str = Field(
        default="0.0.0.0",
        description="Interface the gateway server binds.",
    )
    host_key_path: Path = Field(
        default_factory=lambda: Path.home() / ".drukbox" / "gateway_host_key",
        description=(
            "Private host key of the gateway server. The server makes one "
            "at start when the file does not exist."
        ),
    )
