from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DockerSbxSettings(BaseSettings):
    """Docker Sandboxes (sbx) provider configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DOCKER_SBX_",
        extra="ignore",
    )

    default_image: str = Field(
        default="ghcr.io/czpython/drukbox/sbx-sandbox:latest",
        description="Template image that contains sshd. Build images/sbx/ to change it.",
    )
    sftp_server_command: str = Field(
        default="exec /usr/lib/openssh/sftp-server",
        description=(
            "Command that starts the OpenSSH SFTP server in the image. The "
            "gateway runs it to serve SFTP. The default fits the standard "
            "image; set it for an image with a different path."
        ),
    )
    ssh_username: str = Field(
        default="root",
        description="User in the sandbox for caller SSH access.",
    )
    bootstrap_ssh_timeout_seconds: float = Field(
        default=30.0,
        description="Time limit for the ssh-keyscan tries on a new sandbox.",
    )
    cpus: int = Field(
        default=2,
        ge=1,
        description="Number of CPUs for each sandbox. The daemon default is all host CPUs.",
    )
    memory: str = Field(
        default="2g",
        description=(
            "Memory for each sandbox, in binary units. The daemon default is "
            "half of the host memory."
        ),
    )
    workspace_root: Path = Field(
        default_factory=lambda: Path.home() / ".drukbox" / "sbx-workspaces",
        description=(
            "Directory that holds one temporary workspace for each sandbox. The "
            "daemon reads workspace paths on its own filesystem. When drukbox "
            "runs in a container, the path must be the same in the container "
            "and on the host."
        ),
    )
