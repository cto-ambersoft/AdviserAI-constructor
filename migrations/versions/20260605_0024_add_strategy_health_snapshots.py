"""add strategy_health_snapshots for KPI-Guard history (W9)

Revision ID: 20260605_0024
Revises: 20260604_0023
Create Date: 2026-06-05 00:00:00

W9 of Milestone 4 — Risk Enforcement (AC#4), Phase 0.

Persists the Strategy Health Score as an append-only time series so the W9
KPI-Guard can decide on *recent history* (not just an instant) and the AC#7
dashboard can render a trend. There is intentionally no unique key — every
sweep tick appends a row stamped with the snapshot's ``computed_at`` — so the
KPI-Guard cron and the on-close fast path can never collide on a constraint
(unlike the W8 freshness upsert, I7). "Latest per config" is the
``(config_id, computed_at)`` composite index.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260605_0024"
down_revision: str | None = "20260604_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "strategy_health_snapshots"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE in _table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "config_id",
            sa.Integer(),
            sa.ForeignKey("auto_trade_configs.id"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("win_rate_pct", sa.Float(), nullable=False),
        sa.Column("max_dd_pct", sa.Float(), nullable=False),
        sa.Column("total_pnl_usdt", sa.Float(), nullable=False),
        sa.Column("roi_pct", sa.Float(), nullable=False),
        sa.Column("sharpe_proxy", sa.Float(), nullable=False),
        sa.Column("stability_score", sa.Float(), nullable=False),
        sa.Column("health_score", sa.Float(), nullable=False),
        sa.Column("health_class", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_strategy_health_snapshots_user_id", _TABLE, ["user_id"])
    op.create_index(
        "ix_strategy_health_snapshots_config_computed",
        _TABLE,
        ["config_id", "computed_at"],
    )


def downgrade() -> None:
    if _TABLE in _table_names():
        op.drop_table(_TABLE)
