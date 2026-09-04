from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretProxySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SECRET_PROXY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8781, ge=1, le=65535)
    tunnel_target_host: str = Field(default="127.0.0.1", min_length=1)
    tunnel_box_port: int = Field(default=8781, ge=1, le=65535)
    tunnel_key_path: Path = Path("~/.drukbox/secret-proxy/tunnel_key")
    tunnel_connect_timeout_seconds: float = Field(default=30.0, gt=0)
    tunnel_reconcile_interval_seconds: float = Field(default=5.0, gt=0)
    tunnel_keepalive_interval_seconds: float = Field(default=15.0, gt=0)
    tunnel_keepalive_count_max: int = Field(default=3, gt=0)

    @property
    def expanded_tunnel_key_path(self) -> Path:
        return self.tunnel_key_path.expanduser()
