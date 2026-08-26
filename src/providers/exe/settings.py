from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExeSettings(BaseSettings):
    """exe.dev provider configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="EXE_",
        extra="ignore",
    )

    api_url: str = Field(
        default="https://exe.dev",
        description="Base URL for the exe.dev exec API.",
    )
    api_token: str = Field(
        description="Bearer token used to call the exe.dev exec API.",
    )
    default_image: str = Field(
        description="Default VM image passed to exe.dev when provisioning.",
    )
    image_registry: str | None = Field(
        default=None,
        description=(
            "Repository prefix for derived template images. A VM created from "
            "this registry gets --registry-auth so exe.dev can pull a private image."
        ),
    )
    registry_username: str | None = Field(
        default=None,
        description="Username for the derived-template image registry.",
    )
    registry_password: str | None = Field(
        default=None,
        description="Password or token for the derived-template image registry.",
    )
    api_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for exe.dev API calls.",
    )
    bootstrap_ssh_timeout_seconds: float = Field(
        default=30.0,
        description="ssh-keyscan retry budget for a freshly-joined sandbox on exe.dev.",
    )
    ssh_username: str = Field(
        default="exedev",
        description="In-VM user callers SSH as. Override if the image uses a non-default user.",
    )
