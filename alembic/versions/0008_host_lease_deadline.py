"""Store the provider lifetime limit for each host.

Revision ID: 0008_host_lease_deadline
Revises: 0007_host_service_account
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_host_lease_deadline"
down_revision: str | None = "0007_host_service_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("lease_deadline", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "lease_deadline")
