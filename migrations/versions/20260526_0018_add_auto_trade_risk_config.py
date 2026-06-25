"""add auto_trade_risk_configs for the Pre-Trade Risk Engine

Revision ID: 20260526_0018
Revises: 20260514_0017
Create Date: 2026-05-26 00:00:00

W8 of Milestone 4 — Pre-Trade Risk Engine.

Adds a single 1:1 satellite table ``auto_trade_risk_configs`` holding the
per-strategy risk limits enforced before any order is placed (daily-loss,
exposure cap, leverage ceiling, max-open-positions, conflicting-signal
policy). The ``config_id`` primary key doubles as the foreign key into
``auto_trade_configs`` (ON DELETE CASCADE), giving a strict one-row-per-config
relationship without an extra surrogate key.

Every limit column is nullable: ``NULL`` means "rule off". A config with no
row here is therefore fail-safe — the engine treats it as every limit off and
never opens a trade because of an absent/under-specified risk row. This keeps
the migration purely additive and safe for existing configs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260526_0018"
down_revision: str | None = "20260514_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _get_table_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def upgrade() -> None:
    if "auto_trade_risk_configs" in _get_table_names():
        return
    op.create_table(
        "auto_trade_risk_configs",
        sa.Column(
            "config_id",
            sa.Integer(),
            sa.ForeignKey("auto_trade_configs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("daily_loss_limit_usdt", sa.Float(), nullable=True),
        sa.Column("daily_loss_limit_pct", sa.Float(), nullable=True),
        sa.Column("max_open_positions", sa.Integer(), nullable=True),
        sa.Column("max_open_positions_per_symbol", sa.Integer(), nullable=True),
        sa.Column("exposure_cap_usdt", sa.Float(), nullable=True),
        sa.Column("leverage_ceiling", sa.Integer(), nullable=True),
        sa.Column(
            "conflicting_signal_policy",
            sa.String(length=16),
            nullable=False,
            server_default="block_opposite",
        ),
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
        sa.CheckConstraint(
            "daily_loss_limit_usdt IS NULL OR daily_loss_limit_usdt >= 0",
            name="ck_at_risk_daily_loss_usdt_nonneg",
        ),
        sa.CheckConstraint(
            "daily_loss_limit_pct IS NULL OR daily_loss_limit_pct > 0",
            name="ck_at_risk_daily_loss_pct_pos",
        ),
        sa.CheckConstraint(
            "max_open_positions IS NULL OR max_open_positions >= 1",
            name="ck_at_risk_max_open_min",
        ),
        sa.CheckConstraint(
            "max_open_positions_per_symbol IS NULL OR max_open_positions_per_symbol >= 1",
            name="ck_at_risk_max_open_sym_min",
        ),
        sa.CheckConstraint(
            "exposure_cap_usdt IS NULL OR exposure_cap_usdt > 0",
            name="ck_at_risk_exposure_pos",
        ),
        sa.CheckConstraint(
            "leverage_ceiling IS NULL OR leverage_ceiling >= 1",
            name="ck_at_risk_leverage_min",
        ),
        sa.CheckConstraint(
            "conflicting_signal_policy IN ('block_opposite', 'net', 'replace')",
            name="ck_at_risk_conflicting_policy",
        ),
    )


def downgrade() -> None:
    if "auto_trade_risk_configs" in _get_table_names():
        op.drop_table("auto_trade_risk_configs")
