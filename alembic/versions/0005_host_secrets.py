"""encrypted host secrets

Revision ID: 0005_host_secrets
Revises: 0004_templates
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_host_secrets"
down_revision: str | None = "0004_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        empty_binary = sa.text("decode('', 'hex')")
    elif dialect == "sqlite":
        empty_binary = sa.text("X''")
    else:
        raise RuntimeError(f"unsupported database dialect: {dialect}")

    op.add_column(
        "hosts",
        sa.Column(
            "secrets",
            sa.LargeBinary(),
            nullable=False,
            server_default=empty_binary,
        ),
    )


def downgrade() -> None:
    op.drop_column("hosts", "secrets")
