"""only real accounts have to be unique

A newly created account waits at login 0 / server 'pending' until the trader
connects it. Under the old constraint that placeholder counted, so a trader who
abandoned the connect dialog once could never add another account -- the next
attempt failed on a unique violation. The constraint now ignores placeholders.

Revision ID: c40a7e1b92d5
Revises: 8f21c4d9b0a3
Create Date: 2026-09-16 14:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = 'c40a7e1b92d5'
down_revision = '8f21c4d9b0a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_account_login", "accounts", type_="unique")
    op.create_index(
        "uq_account_login",
        "accounts",
        ["user_id", "mt5_login", "broker_server"],
        unique=True,
        postgresql_where="mt5_login > 0",
    )


def downgrade() -> None:
    op.drop_index("uq_account_login", table_name="accounts")
    op.create_unique_constraint(
        "uq_account_login", "accounts", ["user_id", "mt5_login", "broker_server"]
    )
