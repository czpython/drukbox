from ipaddress import IPv4Address, IPv6Address

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DockerSettings(BaseSettings):
    """Local Docker provider configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DOCKER_",
        extra="ignore",
    )

    default_image: str = Field(
        default="ghcr.io/czpython/drukbox/sandbox:latest",
        description="Sandbox image with sshd; auto-pulled. Build images/local/ to customize.",
    )
    ssh_username: str = Field(
        default="root",
        description="In-container user callers SSH as. The sandbox image runs sshd for root.",
    )
    ssh_host: IPv4Address | IPv6Address = Field(
        default=IPv4Address("127.0.0.1"),
        description="Daemon host address where Docker publishes sshd and callers dial it.",
    )
    bootstrap_ssh_timeout_seconds: float = Field(
        default=30.0,
        description="ssh-keyscan retry budget for a freshly-started sandbox container.",
    )

    @field_validator("ssh_host")
    @classmethod
    def reject_unspecified(cls, address: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
        if address.is_unspecified:
            raise ValueError("DOCKER_SSH_HOST must be an address callers can dial, not 0.0.0.0")
        return address
