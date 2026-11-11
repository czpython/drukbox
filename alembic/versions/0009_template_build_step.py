"""Record the step a template build is in."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_template_build_step"
down_revision: str | None = "0008_template_base_image_ref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "templates", sa.Column("build_step", sa.Text(), nullable=False, server_default="")
    )


def downgrade() -> None:
    with op.batch_alter_table("templates") as templates:
        templates.drop_column("build_step")
