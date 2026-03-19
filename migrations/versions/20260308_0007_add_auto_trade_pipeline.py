"""add auto trade pipeline tables

Revision ID: 20260308_0007
Revises: 20260305_0006
Create Date: 2026-03-08 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260308_0007"
down_revision: str | None = "20260305_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auto_trade_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_running", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position_size_usdt", sa.Float(), nullable=False, server_default="100"),
        sa.Column("leverage", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("min_confidence_pct", sa.Float(), nullable=False, server_default="62"),
        sa.Column("fast_close_confidence_pct", sa.Float(), nullable=False, server_default="80"),
        sa.Column("confirm_reports_required", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("risk_mode", sa.String(length=8), nullable=False, server_default="1:2"),
        sa.Column("sl_pct", sa.Float(), nullable=False, server_default="1"),
        sa.Column("tp_pct", sa.Float(), nullable=False, server_default="2"),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_stopped_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["account_id"], ["exchange_credentials.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["personal_analysis_profiles.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_auto_trade_configs_user_id"),
        sa.CheckConstraint(
            "position_size_usdt > 0",
            name="ck_auto_trade_cfg_position_size_positive",
        ),
        sa.CheckConstraint("leverage >= 1", name="ck_auto_trade_cfg_leverage_min"),
        sa.CheckConstraint(
            "min_confidence_pct >= 0 AND min_confidence_pct <= 100",
            name="ck_auto_trade_cfg_min_confidence_bounds",
        ),
        sa.CheckConstraint(
            "fast_close_confidence_pct >= 0 AND fast_close_confidence_pct <= 100",
            name="ck_auto_trade_cfg_fast_close_confidence_bounds",
        ),
        sa.CheckConstraint(
            "confirm_reports_required >= 1",
            name="ck_auto_trade_cfg_confirm_reports_required_min",
        ),
        sa.CheckConstraint("risk_mode IN ('1:2', '1:3')", name="ck_auto_trade_cfg_risk_mode"),
        sa.CheckConstraint("sl_pct > 0", name="ck_auto_trade_cfg_sl_pct_positive"),
        sa.CheckConstraint("tp_pct > 0", name="ck_auto_trade_cfg_tp_pct_positive"),
    )
    op.create_index(
        "ix_auto_trade_configs_user_running",
        "auto_trade_configs",
        ["user_id", "enabled", "is_running"],
        unique=False,
    )
    op.create_index(
        "ix_auto_trade_configs_profile_id",
        "auto_trade_configs",
        ["profile_id"],
        unique=False,
    )

    op.create_table(
        "auto_trade_positions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("position_size_usdt", sa.Float(), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("tp_price", sa.Float(), nullable=False),
        sa.Column("sl_price", sa.Float(), nullable=False),
        sa.Column("entry_confidence_pct", sa.Float(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=64), nullable=True),
        sa.Column("close_price", sa.Float(), nullable=True),
        sa.Column("open_order_id", sa.String(length=128), nullable=True),
        sa.Column("close_order_id", sa.String(length=128), nullable=True),
        sa.Column("open_history_id", sa.Integer(), nullable=True),
        sa.Column("close_history_id", sa.Integer(), nullable=True),
        sa.Column("raw_open_order", sa.JSON(), nullable=False),
        sa.Column("raw_close_order", sa.JSON(), nullable=False),
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
        sa.ForeignKeyConstraint(["account_id"], ["exchange_credentials.id"]),
        sa.ForeignKeyConstraint(["close_history_id"], ["personal_analysis_history.id"]),
        sa.ForeignKeyConstraint(["config_id"], ["auto_trade_configs.id"]),
        sa.ForeignKeyConstraint(["open_history_id"], ["personal_analysis_history.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["personal_analysis_profiles.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("quantity > 0", name="ck_auto_trade_pos_quantity_positive"),
        sa.CheckConstraint("entry_price > 0", name="ck_auto_trade_pos_entry_price_positive"),
        sa.CheckConstraint(
            "position_size_usdt > 0",
            name="ck_auto_trade_pos_position_size_positive",
        ),
        sa.CheckConstraint("leverage >= 1", name="ck_auto_trade_pos_leverage_min"),
        sa.CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_auto_trade_pos_side"),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'error')",
            name="ck_auto_trade_pos_status",
        ),
    )
    op.create_index(
        "ix_auto_trade_positions_user_id",
        "auto_trade_positions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_auto_trade_positions_user_status",
        "auto_trade_positions",
        ["user_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_auto_trade_positions_user_open",
        "auto_trade_positions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_auto_trade_positions_open_order_id",
        "auto_trade_positions",
        ["open_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_auto_trade_positions_close_order_id",
        "auto_trade_positions",
        ["close_order_id"],
        unique=False,
    )

    op.create_table(
        "auto_trade_signal_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("last_processed_history_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_trend", sa.String(length=16), nullable=True),
        sa.Column("opposite_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_signal_confidence_pct", sa.Float(), nullable=True),
        sa.Column("last_signal_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["config_id"], ["auto_trade_configs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("config_id", name="uq_auto_trade_signal_state_config_id"),
        sa.UniqueConstraint("user_id", name="uq_auto_trade_signal_state_user_id"),
    )
    op.create_index(
        "ix_auto_trade_signal_state_user_id",
        "auto_trade_signal_state",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "auto_trade_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=True),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("history_id", sa.Integer(), nullable=True),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["config_id"], ["auto_trade_configs.id"]),
        sa.ForeignKeyConstraint(["history_id"], ["personal_analysis_history.id"]),
        sa.ForeignKeyConstraint(["position_id"], ["auto_trade_positions.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["personal_analysis_profiles.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auto_trade_events_user_id_created_at",
        "auto_trade_events",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_auto_trade_events_event_type",
        "auto_trade_events",
        ["event_type"],
        unique=False,
    )

    op.create_table(
        "auto_trade_signal_queue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("history_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["config_id"], ["auto_trade_configs.id"]),
        sa.ForeignKeyConstraint(["history_id"], ["personal_analysis_history.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["personal_analysis_profiles.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("history_id", name="uq_auto_trade_signal_queue_history_id"),
        sa.CheckConstraint("attempt >= 0", name="ck_auto_trade_queue_attempt_min"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_auto_trade_queue_max_attempts_min"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'dead')",
            name="ck_auto_trade_queue_status",
        ),
    )
    op.create_index(
        "ix_auto_trade_signal_queue_status_next_retry_at",
        "auto_trade_signal_queue",
        ["status", "next_retry_at"],
        unique=False,
    )
    op.create_index(
        "ix_auto_trade_signal_queue_user_status",
        "auto_trade_signal_queue",
        ["user_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auto_trade_signal_queue_user_status",
        table_name="auto_trade_signal_queue",
    )
    op.drop_index(
        "ix_auto_trade_signal_queue_status_next_retry_at",
        table_name="auto_trade_signal_queue",
    )
    op.drop_table("auto_trade_signal_queue")

    op.drop_index("ix_auto_trade_events_event_type", table_name="auto_trade_events")
    op.drop_index("ix_auto_trade_events_user_id_created_at", table_name="auto_trade_events")
    op.drop_table("auto_trade_events")

    op.drop_index("ix_auto_trade_signal_state_user_id", table_name="auto_trade_signal_state")
    op.drop_table("auto_trade_signal_state")

    op.drop_index("ix_auto_trade_positions_close_order_id", table_name="auto_trade_positions")
    op.drop_index("ix_auto_trade_positions_open_order_id", table_name="auto_trade_positions")
    op.drop_index("uq_auto_trade_positions_user_open", table_name="auto_trade_positions")
    op.drop_index("ix_auto_trade_positions_user_status", table_name="auto_trade_positions")
    op.drop_index("ix_auto_trade_positions_user_id", table_name="auto_trade_positions")
    op.drop_table("auto_trade_positions")

    op.drop_index("ix_auto_trade_configs_profile_id", table_name="auto_trade_configs")
    op.drop_index("ix_auto_trade_configs_user_running", table_name="auto_trade_configs")
    op.drop_table("auto_trade_configs")
