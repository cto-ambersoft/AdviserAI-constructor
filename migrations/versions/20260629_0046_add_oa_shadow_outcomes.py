"""add oa_shadow_outcomes table (Outcome-Aware counterfactual outcomes)

Revision ID: 20260629_0046
Revises: 20260628_0045
Create Date: 2026-06-29 12:00:00

Shadow (counterfactual) outcome per personal-analysis forecast (S2). Captures the
predicted direction/confidence and a horizon; a Taskiq backfill later fills
``realized_move_pct`` from OHLCV using point-in-time as-of lookups. This lets OA
learn from forecasts that were never entered (defeats selection bias, spec §5.3).
Purely additive — a new table, one row per forecast (``history_id`` unique).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260629_0046"
down_revision: str | None = "20260628_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "oa_shadow_outcomes"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("history_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("decision_event_id", sa.String(length=64), nullable=True),
        sa.Column("signal_time_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_direction", sa.String(length=8), nullable=False),
        sa.Column("predicted_conf", sa.Float(), nullable=True),
        sa.Column("realized_move_pct", sa.Float(), nullable=True),
        sa.Column("entered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["personal_analysis_profiles.id"]),
        sa.ForeignKeyConstraint(["history_id"], ["personal_analysis_history.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{_TABLE}_user_id", _TABLE, ["user_id"], unique=False)
    op.create_index(f"ix_{_TABLE}_profile_id", _TABLE, ["profile_id"], unique=False)
    op.create_index(f"ix_{_TABLE}_history_id", _TABLE, ["history_id"], unique=True)
    op.create_index(f"ix_{_TABLE}_symbol", _TABLE, ["symbol"], unique=False)
    op.create_index(
        f"ix_{_TABLE}_decision_event_id", _TABLE, ["decision_event_id"], unique=False
    )
    op.create_index(
        f"ix_{_TABLE}_signal_time_utc", _TABLE, ["signal_time_utc"], unique=False
    )
    op.create_index(
        f"ix_{_TABLE}_horizon_end_utc", _TABLE, ["horizon_end_utc"], unique=False
    )
    op.create_index(f"ix_{_TABLE}_entered", _TABLE, ["entered"], unique=False)


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_entered", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_horizon_end_utc", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_signal_time_utc", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_decision_event_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_symbol", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_history_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_profile_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
