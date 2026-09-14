"""Store service account token fingerprints.

Revision ID: 0006_service_accounts
Revises: 0005_host_secrets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_service_accounts"
down_revision: str | None = "0005_host_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_accounts",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
    )


def downgrade() -> None:
    op.drop_table("service_accounts")
