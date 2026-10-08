"""Record the base image reference used for each template."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_template_base_image_ref"
down_revision: str | None = "0007_host_service_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("templates", sa.Column("base_image_ref", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE templates SET base_image_ref = base_image"))
    with op.batch_alter_table("templates") as templates:
        templates.alter_column("base_image_ref", nullable=False)
        templates.drop_index("ix_templates_provider_base_image_setup_script_hash")
        templates.create_index(
            "ix_templates_provider_base_image_ref_setup_script_hash",
            ["provider", "base_image", "base_image_ref", "setup_script_hash"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("templates") as templates:
        templates.drop_index("ix_templates_provider_base_image_ref_setup_script_hash")
        templates.drop_column("base_image_ref")
        templates.create_index(
            "ix_templates_provider_base_image_setup_script_hash",
            ["provider", "base_image", "setup_script_hash"],
            unique=True,
        )
