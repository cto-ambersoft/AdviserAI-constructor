"""add auto_trade_configs risk-off latch columns (A3 — audit §2.5.9)

Revision ID: 20260625_0042
Revises: 20260621_0041
Create Date: 2026-06-25 12:00:00

Persist the Volatility Kill-Switch risk-off latch so it survives a process
restart. The kill-switch already pauses the strategy (``is_running=False``);
these columns record *why* (``risk_off_reason``) and *when* (``risk_off_at``)
plus an explicit boolean (``risk_off_latched``) an operator/UI can read back.
Cleared on a manual resume. Purely additive: existing rows default to "not
latched". Portable via batch mode (native ALTER on PostgreSQL, recreate on
SQLite).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260625_0042"
down_revision: str | None = "20260621_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_configs"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "risk_off_latched",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("risk_off_reason", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("risk_off_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("risk_off_at")
        batch_op.drop_column("risk_off_reason")
        batch_op.drop_column("risk_off_latched")
