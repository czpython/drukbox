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
    bind_port: int = Field(default=8781, ge=0, le=65535)
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

    control_socket: Path = Path("~/.drukbox/secret-proxy/control.sock")
    certificate_directory: Path = Path("~/.drukbox/secret-proxy/certificates")
    allow_private_upstreams: bool = False
    upstream_timeout_seconds: float = Field(default=60.0, gt=0)

    @property
    def expanded_control_socket(self) -> Path:
        return self.control_socket.expanduser()

    @property
    def expanded_certificate_directory(self) -> Path:
        return self.certificate_directory.expanduser()
