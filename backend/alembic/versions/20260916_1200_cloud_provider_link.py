"""cloud history provider link

Adds the handle for an account whose MetaTrader terminal is hosted by a cloud
provider. Deliberately no credential column: the provider holds the read-only
investor password, so the journal database still contains no MT5 password.

Revision ID: 8f21c4d9b0a3
Revises: 51c8a494a701
Create Date: 2026-09-16 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '8f21c4d9b0a3'
down_revision = '51c8a494a701'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("provider", sa.String(length=16), nullable=True))
    op.add_column(
        "accounts", sa.Column("provider_account_id", sa.String(length=64), nullable=True)
    )
    op.add_column("accounts", sa.Column("provider_region", sa.String(length=32), nullable=True))
    op.add_column("accounts", sa.Column("provider_state", sa.String(length=24), nullable=True))
    op.add_column(
        "accounts",
        sa.Column("provider_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_accounts_provider_account_id", "accounts", ["provider_account_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_provider_account_id", table_name="accounts")
    op.drop_column("accounts", "provider_synced_at")
    op.drop_column("accounts", "provider_state")
    op.drop_column("accounts", "provider_region")
    op.drop_column("accounts", "provider_account_id")
    op.drop_column("accounts", "provider")
