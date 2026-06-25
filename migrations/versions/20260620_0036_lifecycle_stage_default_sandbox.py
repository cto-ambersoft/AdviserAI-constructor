"""flip auto_trade_configs.lifecycle_stage default live -> sandbox (T8 — W10e)

Revision ID: 20260620_0036
Revises: 20260618_0035
Create Date: 2026-06-20 12:00:00

M4 remediation T8 (audit W10e). The lifecycle_stage column shipped with
``server_default='live'`` as a zero-behavior-change placeholder. Now that the
sandbox execution guard (P4-4) and KPI gate are in place, the fail-safe default
is flipped to ``sandbox``: a config created by *any* path (not just
``upsert_config``) must never default into real-money ``live``.

This changes ONLY the column default for future inserts. Existing rows are left
untouched — strategies already running in ``live`` stay ``live`` (no backfill).
Idempotent and portable via batch mode (native ALTER on PostgreSQL, recreate on
SQLite).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260620_0036"
down_revision: str | None = "20260618_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_configs"
_COLUMN = "lifecycle_stage"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.String(length=16),
            existing_nullable=False,
            server_default="sandbox",
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.String(length=16),
            existing_nullable=False,
            server_default="live",
        )
