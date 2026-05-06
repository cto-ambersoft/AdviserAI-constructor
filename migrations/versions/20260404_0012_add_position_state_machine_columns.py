"""add position state machine columns to auto_trade_positions

Revision ID: 20260404_0012
Revises: 20260325_0011
Create Date: 2026-04-04 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260404_0012"
down_revision: str | None = "20260325_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auto_trade_positions",
        sa.Column("state", sa.String(length=30), nullable=False, server_default="open"),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("original_quantity", sa.Numeric(precision=20, scale=8), nullable=True),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("current_quantity", sa.Numeric(precision=20, scale=8), nullable=True),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("sl_type", sa.String(length=20), server_default="fixed"),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("sl_exchange_order_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("sl_history_json", sa.JSON(), server_default=sa.text("'[]'")),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("tp_mode", sa.String(length=10), server_default="single"),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("tp_levels_json", sa.JSON(), server_default=sa.text("'[]'")),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("tp_history_json", sa.JSON(), server_default=sa.text("'[]'")),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("trailing_config_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("breakeven_config_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("volatility_config_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("active_watchers_json", sa.JSON(), server_default=sa.text("'[]'")),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column(
            "adjustment_priority_json",
            sa.JSON(),
            server_default=sa.text("'[\"watcher\",\"trailing\",\"breakeven\",\"volatility\"]'"),
        ),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("transition_log_json", sa.JSON(), server_default=sa.text("'[]'")),
    )
    op.add_column(
        "auto_trade_positions",
        sa.Column("last_adjusted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Ensure existing rows are fully backfilled even on dialects with edge-case
    # behavior around defaults when adding columns.
    op.execute(sa.text("UPDATE auto_trade_positions SET state = 'open' WHERE state IS NULL"))
    op.execute(sa.text("UPDATE auto_trade_positions SET sl_type = 'fixed' WHERE sl_type IS NULL"))
    op.execute(sa.text("UPDATE auto_trade_positions SET sl_history_json = '[]' WHERE sl_history_json IS NULL"))
    op.execute(sa.text("UPDATE auto_trade_positions SET tp_mode = 'single' WHERE tp_mode IS NULL"))
    op.execute(sa.text("UPDATE auto_trade_positions SET tp_levels_json = '[]' WHERE tp_levels_json IS NULL"))
    op.execute(sa.text("UPDATE auto_trade_positions SET tp_history_json = '[]' WHERE tp_history_json IS NULL"))
    op.execute(
        sa.text(
            "UPDATE auto_trade_positions SET active_watchers_json = '[]' "
            "WHERE active_watchers_json IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE auto_trade_positions "
            "SET adjustment_priority_json = '[\"watcher\",\"trailing\",\"breakeven\",\"volatility\"]' "
            "WHERE adjustment_priority_json IS NULL"
        )
    )
    op.execute(
        sa.text("UPDATE auto_trade_positions SET transition_log_json = '[]' WHERE transition_log_json IS NULL")
    )

    op.create_index(
        "ix_positions_user_state",
        "auto_trade_positions",
        ["user_id", "state"],
        unique=False,
        postgresql_where=sa.text("state NOT IN ('closed', 'cancelled', 'failed')"),
        sqlite_where=sa.text("state NOT IN ('closed', 'cancelled', 'failed')"),
    )


def downgrade() -> None:
    op.drop_index("ix_positions_user_state", table_name="auto_trade_positions")

    op.drop_column("auto_trade_positions", "last_adjusted_at")
    op.drop_column("auto_trade_positions", "transition_log_json")
    op.drop_column("auto_trade_positions", "adjustment_priority_json")
    op.drop_column("auto_trade_positions", "active_watchers_json")
    op.drop_column("auto_trade_positions", "volatility_config_json")
    op.drop_column("auto_trade_positions", "breakeven_config_json")
    op.drop_column("auto_trade_positions", "trailing_config_json")
    op.drop_column("auto_trade_positions", "tp_history_json")
    op.drop_column("auto_trade_positions", "tp_levels_json")
    op.drop_column("auto_trade_positions", "tp_mode")
    op.drop_column("auto_trade_positions", "sl_history_json")
    op.drop_column("auto_trade_positions", "sl_exchange_order_id")
    op.drop_column("auto_trade_positions", "sl_type")
    op.drop_column("auto_trade_positions", "current_quantity")
    op.drop_column("auto_trade_positions", "original_quantity")
    op.drop_column("auto_trade_positions", "state")
