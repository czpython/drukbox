"""reusable provider templates

Revision ID: 0004_templates
Revises: 0003_host_public_key
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_templates"
down_revision: str | None = "0003_host_public_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("base_image", sa.Text(), nullable=False),
        sa.Column("requirements_hash", sa.String(length=64), nullable=False),
        sa.Column("setup_script", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("image", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_templates_provider_base_image_requirements_hash",
        "templates",
        ["provider", "base_image", "requirements_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_templates_provider_base_image_requirements_hash",
        table_name="templates",
    )
    op.drop_table("templates")
