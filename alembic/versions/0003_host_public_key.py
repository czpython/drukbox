"""per-host public key for the SSH gateway

Revision ID: 0003_host_public_key
Revises: 0002_host_sizing
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_host_public_key"
down_revision: str | None = "0002_host_sizing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("public_key", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("hosts", "public_key")
