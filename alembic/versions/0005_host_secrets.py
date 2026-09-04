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
    op.add_column("hosts", sa.Column("secrets", sa.LargeBinary(), nullable=True))
    hosts = sa.table("hosts", sa.column("secrets", sa.LargeBinary()))
    op.get_bind().execute(hosts.update().values(secrets=b""))
    with op.batch_alter_table("hosts") as batch:
        batch.alter_column("secrets", existing_type=sa.LargeBinary(), nullable=False)


def downgrade() -> None:
    op.drop_column("hosts", "secrets")
