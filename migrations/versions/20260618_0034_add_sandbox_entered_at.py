"""add sandbox_entered_at to auto_trade_configs (B5 — W10 Promotion)

Revision ID: 20260618_0034
Revises: 20260618_0033
Create Date: 2026-06-18 11:30:00

Phase 4 of Milestone 4 — Strategy Promotion Pipeline (W10).

Records when a strategy entered the sandbox stage, so the KPI Gate can enforce a
minimum sandbox period before promotion. Nullable: NULL ⇒ not in (or never put
into) sandbox; the service falls back to ``created_at`` when computing the
sandbox tenure. Set on every transition *into* sandbox (creation in sandbox once
P4-4 lands, and demote live→sandbox).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260618_0034"
down_revision: str | None = "20260618_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_configs"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column("sandbox_entered_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("sandbox_entered_at")
