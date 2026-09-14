"""Record the service account that holds each host.

Revision ID: 0007_host_service_account
Revises: 0006_service_accounts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_host_service_account"
down_revision: str | None = "0006_service_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service_accounts") as service_accounts:
        service_accounts.alter_column("fingerprint", nullable=True)
    op.execute(sa.text("INSERT INTO service_accounts (name) VALUES ('admin')"))
    op.add_column("hosts", sa.Column("service_account", sa.String(64)))
    # Before this revision only admin keys could create or claim a host.
    op.execute(
        sa.text(
            "UPDATE hosts SET service_account = 'admin' "
            "WHERE NOT pool_member OR claimed_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("hosts", "service_account")
    op.execute(sa.text("DELETE FROM service_accounts WHERE name = 'admin'"))
    with op.batch_alter_table("service_accounts") as service_accounts:
        service_accounts.alter_column("fingerprint", nullable=False)
