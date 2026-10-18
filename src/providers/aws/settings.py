from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import CsvTuple


class AwsSettings(BaseSettings):
    """AWS EC2 provider configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="AWS_",
        extra="ignore",
        populate_by_name=True,
    )

    region: str = Field(description="AWS region for the EC2 client and instance launch.")
    default_image: str = Field(
        description="Default image when the caller doesn't pass `image`. AMI id or SSM path.",
    )
    instance_type: str = Field(default="t3.medium", description="EC2 instance type.")
    root_gb: int = Field(default=100, description="Root EBS volume size in GB (gp3, encrypted).")
    subnet_id: str | None = Field(
        default=None, description="Optional VPC subnet; falls back to the default VPC's."
    )
    security_group_id: str | None = Field(
        default=None,
        description="Pre-existing SG. Unset → drukbox manages `drukbox-managed`.",
    )
    ssh_cidrs: CsvTuple = Field(
        default=(),
        description=(
            "CIDRs for SSH ingress. Authoritative when set: drukbox reconciles "
            "tcp/22 on the managed group to match, revoking stale rules. "
            "Unset → detected egress /32, falling back to 0.0.0.0/0."
        ),
    )
    instance_profile: str | None = Field(
        default=None, description="Optional IAM instance profile name."
    )
    bootstrap_ssh_timeout_seconds: float = Field(
        default=120.0,
        description="ssh-keyscan retry budget for a freshly-launched EC2 instance.",
    )
    ssh_username: str = Field(
        default="ubuntu",
        description="In-VM user callers SSH as. Override if the AMI uses a non-default user.",
    )
    tailnet: bool = Field(
        default=False,
        description=(
            "Join each host to the tailnet. The host then has no key pair, "
            "security group, or public IP."
        ),
    )
