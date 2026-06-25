"""add Volatility Kill-Switch thresholds to auto_trade_risk_configs (W9)

Revision ID: 20260605_0026
Revises: 20260605_0025
Create Date: 2026-06-05 01:00:00

W9 of Milestone 4 — Risk Enforcement (AC#4), Phase 2.

Extends the risk-config satellite with the Volatility Kill-Switch thresholds the
in-trade hard auto-close reads. ``kill_switch_enabled`` is the master switch
(default ``false`` — fully opt-in); the params are nullable (``NULL`` ⇒ that
branch off, or the detector's engine default for atr_period / cooldown). Bound
CHECKs mirror the API schema (as in 0020/0025).

Batch mode keeps it portable: native ``ALTER TABLE`` on PostgreSQL, table
recreate on SQLite (triggered by the CHECK constraints, per recreate="auto").
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260605_0026"
down_revision: str | None = "20260605_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_risk_configs"

_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_at_risk_ks_atr_spike_mult_min",
        "kill_switch_atr_spike_mult IS NULL OR kill_switch_atr_spike_mult > 1",
    ),
    (
        "ck_at_risk_ks_atr_period_min",
        "kill_switch_atr_period IS NULL OR kill_switch_atr_period >= 2",
    ),
    (
        "ck_at_risk_ks_price_move_pos",
        "kill_switch_price_move_pct IS NULL OR kill_switch_price_move_pct > 0",
    ),
    (
        "ck_at_risk_ks_cooldown_nonneg",
        "kill_switch_cooldown_seconds IS NULL OR kill_switch_cooldown_seconds >= 0",
    ),
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kill_switch_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("kill_switch_atr_spike_mult", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("kill_switch_atr_period", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("kill_switch_price_move_pct", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("kill_switch_cooldown_seconds", sa.Integer(), nullable=True))
        for name, condition in _CHECKS:
            batch_op.create_check_constraint(name, condition)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for name, _ in reversed(_CHECKS):
            batch_op.drop_constraint(name, type_="check")
        batch_op.drop_column("kill_switch_cooldown_seconds")
        batch_op.drop_column("kill_switch_price_move_pct")
        batch_op.drop_column("kill_switch_atr_period")
        batch_op.drop_column("kill_switch_atr_spike_mult")
        batch_op.drop_column("kill_switch_enabled")
