"""add KPI-Guard auto-pause thresholds to auto_trade_risk_configs (W9)

Revision ID: 20260605_0025
Revises: 20260605_0024
Create Date: 2026-06-05 00:30:00

W9 of Milestone 4 — Risk Enforcement (AC#4), Phase 1.

Extends the 1:1 risk-config satellite with the KPI-Guard thresholds the W9
auto-pause reads. These are DISTINCT from the pre-trade ``daily_loss_limit_*``
already on the table: those *block the next entry*; these *pause the whole
strategy* when live KPIs breach, and are meant to be set more conservatively.

All thresholds are nullable (``NULL`` = rule off) and ``kpi_guard_enabled``
defaults to ``false`` (master switch off) — so the guard is fully opt-in and a
legacy/under-specified row never auto-pauses. Upper/lower bound CHECKs mirror the
API schema so the DB is a backstop for non-Pydantic writers (as in 0020).

Batch mode keeps it portable: native ``ALTER TABLE`` on PostgreSQL, table
recreate on SQLite (triggered by the CHECK constraints, per recreate="auto").
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260605_0025"
down_revision: str | None = "20260605_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_risk_configs"

_CHECKS: tuple[tuple[str, str], ...] = (
    ("ck_at_risk_kpi_max_dd_pos", "kpi_guard_max_dd_pct IS NULL OR kpi_guard_max_dd_pct > 0"),
    ("ck_at_risk_kpi_max_dd_max", "kpi_guard_max_dd_pct IS NULL OR kpi_guard_max_dd_pct <= 100"),
    (
        "ck_at_risk_kpi_daily_loss_usdt_nonneg",
        "kpi_guard_max_daily_loss_usdt IS NULL OR kpi_guard_max_daily_loss_usdt >= 0",
    ),
    (
        "ck_at_risk_kpi_daily_loss_pct_pos",
        "kpi_guard_max_daily_loss_pct IS NULL OR kpi_guard_max_daily_loss_pct > 0",
    ),
    (
        "ck_at_risk_kpi_daily_loss_pct_max",
        "kpi_guard_max_daily_loss_pct IS NULL OR kpi_guard_max_daily_loss_pct <= 100",
    ),
    (
        "ck_at_risk_kpi_min_wr_nonneg",
        "kpi_guard_min_win_rate_pct IS NULL OR kpi_guard_min_win_rate_pct >= 0",
    ),
    (
        "ck_at_risk_kpi_min_wr_max",
        "kpi_guard_min_win_rate_pct IS NULL OR kpi_guard_min_win_rate_pct <= 100",
    ),
    ("ck_at_risk_kpi_min_trades_min", "kpi_guard_min_trades IS NULL OR kpi_guard_min_trades >= 1"),
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kpi_guard_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("kpi_guard_max_dd_pct", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("kpi_guard_max_daily_loss_usdt", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("kpi_guard_max_daily_loss_pct", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("kpi_guard_min_win_rate_pct", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("kpi_guard_min_trades", sa.Integer(), nullable=True))
        for name, condition in _CHECKS:
            batch_op.create_check_constraint(name, condition)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for name, _ in reversed(_CHECKS):
            batch_op.drop_constraint(name, type_="check")
        batch_op.drop_column("kpi_guard_min_trades")
        batch_op.drop_column("kpi_guard_min_win_rate_pct")
        batch_op.drop_column("kpi_guard_max_daily_loss_pct")
        batch_op.drop_column("kpi_guard_max_daily_loss_usdt")
        batch_op.drop_column("kpi_guard_max_dd_pct")
        batch_op.drop_column("kpi_guard_enabled")
