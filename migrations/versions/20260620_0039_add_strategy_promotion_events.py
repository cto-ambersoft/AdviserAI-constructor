"""add strategy_promotion_events lifecycle-audit table (T19 — W10f)

Revision ID: 20260620_0039
Revises: 20260620_0038
Create Date: 2026-06-20 15:00:00

M4 remediation T19 (audit W10f). A first-class, queryable audit trail of strategy
lifecycle transitions (promote / demote / gate-failure) — complements the
auto_trade_events notification stream with a durable history carrying the stage
transition, decision, KPI-gate snapshot, and actor.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260620_0039"
down_revision: str | None = "20260620_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "strategy_promotion_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("from_stage", sa.String(length=16), nullable=False),
        sa.Column("to_stage", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("kpi_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("actor", sa.String(length=24), nullable=True),
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
        sa.ForeignKeyConstraint(["config_id"], ["auto_trade_configs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{_TABLE}_user_id", _TABLE, ["user_id"])
    op.create_index(f"ix_{_TABLE}_config_id", _TABLE, ["config_id"])
    op.create_index(f"ix_{_TABLE}_decision", _TABLE, ["decision"])
    op.create_index(
        "ix_strategy_promotion_events_config_created", _TABLE, ["config_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_promotion_events_config_created", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_decision", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_config_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
