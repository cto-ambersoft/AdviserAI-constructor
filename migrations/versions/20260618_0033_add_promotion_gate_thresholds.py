"""add Strategy Promotion KPI-Gate thresholds to auto_trade_risk_configs (B5 — W10)

Revision ID: 20260618_0033
Revises: 20260618_0032
Create Date: 2026-06-18 11:00:00

Phase 4 of Milestone 4 — Strategy Promotion Pipeline (W10), the KPI Gate.

Extends the risk-config satellite with the thresholds the sandbox→live gate
(``app/services/auto_trade/promotion/kpi_gate.py``) checks before a strategy may
be promoted. Unlike the kpi_guard limits (``NULL`` ⇒ rule off), a ``NULL`` here
means "use the gate's conservative built-in default" — a promotion gate always
has criteria (you cannot promote against no bar). Bound CHECKs mirror the API
schema.

Batch mode keeps it portable (native ALTER on PostgreSQL, recreate on SQLite).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260618_0033"
down_revision: str | None = "20260618_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_risk_configs"

_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_at_risk_promote_min_wr_bounds",
        "promote_min_win_rate_pct IS NULL OR "
        "(promote_min_win_rate_pct >= 0 AND promote_min_win_rate_pct <= 100)",
    ),
    (
        "ck_at_risk_promote_max_dd_bounds",
        "promote_max_dd_pct IS NULL OR "
        "(promote_max_dd_pct > 0 AND promote_max_dd_pct <= 100)",
    ),
    (
        "ck_at_risk_promote_min_trades_min",
        "promote_min_trades IS NULL OR promote_min_trades >= 1",
    ),
    (
        "ck_at_risk_promote_min_sandbox_days_nonneg",
        "promote_min_sandbox_days IS NULL OR promote_min_sandbox_days >= 0",
    ),
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("promote_min_win_rate_pct", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("promote_max_dd_pct", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("promote_min_trades", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("promote_min_sandbox_days", sa.Float(), nullable=True))
        for name, condition in _CHECKS:
            batch_op.create_check_constraint(name, condition)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for name, _ in reversed(_CHECKS):
            batch_op.drop_constraint(name, type_="check")
        batch_op.drop_column("promote_min_sandbox_days")
        batch_op.drop_column("promote_min_trades")
        batch_op.drop_column("promote_max_dd_pct")
        batch_op.drop_column("promote_min_win_rate_pct")
