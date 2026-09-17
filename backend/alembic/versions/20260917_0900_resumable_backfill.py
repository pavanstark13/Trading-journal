"""a first import that can be resumed

A ten-year history does not fit in one serverless invocation, and a timeout part way
through used to mean nothing was imported at all. The cursor records how far the
import has got so the next run continues rather than starting over.

Revision ID: d5b3f8c07a41
Revises: c40a7e1b92d5
Create Date: 2026-09-17 09:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd5b3f8c07a41'
down_revision = 'c40a7e1b92d5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("provider_backfill_cursor_msc", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "provider_backfill_cursor_msc")
