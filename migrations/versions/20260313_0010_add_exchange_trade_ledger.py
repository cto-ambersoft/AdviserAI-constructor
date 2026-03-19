"""add exchange trade ledger tables

Revision ID: 20260313_0010
Revises: 20260312_0009
Create Date: 2026-03-13 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260313_0010"
down_revision: str | None = "20260312_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exchange_trade_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("exchange_name", sa.String(length=32), nullable=False),
        sa.Column("market_type", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("exchange_trade_id", sa.String(length=128), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=128), nullable=True),
        sa.Column("client_order_id", sa.String(length=128), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.Column("fee_cost", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("fee_currency", sa.String(length=32), nullable=True),
        sa.Column("traded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column(
            "origin_confidence",
            sa.String(length=16),
            nullable=False,
            server_default="none",
        ),
        sa.Column("auto_trade_config_id", sa.Integer(), nullable=True),
        sa.Column("auto_trade_position_id", sa.Integer(), nullable=True),
        sa.Column("open_history_id", sa.Integer(), nullable=True),
        sa.Column("close_history_id", sa.Integer(), nullable=True),
        sa.Column("raw_trade", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["exchange_credentials.id"]),
        sa.ForeignKeyConstraint(["auto_trade_config_id"], ["auto_trade_configs.id"]),
        sa.ForeignKeyConstraint(["auto_trade_position_id"], ["auto_trade_positions.id"]),
        sa.ForeignKeyConstraint(["open_history_id"], ["personal_analysis_history.id"]),
        sa.ForeignKeyConstraint(["close_history_id"], ["personal_analysis_history.id"]),
        sa.CheckConstraint("price >= 0", name="ck_exchange_trade_ledger_price_non_negative"),
        sa.CheckConstraint("amount >= 0", name="ck_exchange_trade_ledger_amount_non_negative"),
        sa.CheckConstraint("fee_cost >= 0", name="ck_exchange_trade_ledger_fee_non_negative"),
        sa.CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_exchange_trade_ledger_market_type",
        ),
        sa.CheckConstraint(
            "origin IN ('platform', 'external', 'unknown')",
            name="ck_exchange_trade_ledger_origin",
        ),
        sa.CheckConstraint(
            "origin_confidence IN ('strong', 'weak', 'none')",
            name="ck_exchange_trade_ledger_origin_confidence",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "exchange_name",
            "market_type",
            "symbol",
            "exchange_trade_id",
            name="uq_exchange_trade_ledger_trade_identity",
        ),
    )
    op.create_index(
        "ix_exchange_trade_ledger_user_id",
        "exchange_trade_ledger",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_account_id",
        "exchange_trade_ledger",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_symbol",
        "exchange_trade_ledger",
        ["symbol"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_auto_trade_config_id",
        "exchange_trade_ledger",
        ["auto_trade_config_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_auto_trade_position_id",
        "exchange_trade_ledger",
        ["auto_trade_position_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_open_history_id",
        "exchange_trade_ledger",
        ["open_history_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_close_history_id",
        "exchange_trade_ledger",
        ["close_history_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_account_symbol_traded_at",
        "exchange_trade_ledger",
        ["account_id", "symbol", "traded_at"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_account_exchange_order_id",
        "exchange_trade_ledger",
        ["account_id", "exchange_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_account_client_order_id",
        "exchange_trade_ledger",
        ["account_id", "client_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_ledger_position_traded_at",
        "exchange_trade_ledger",
        ["auto_trade_position_id", "traded_at"],
        unique=False,
    )

    op.create_table(
        "exchange_trade_sync_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market_type", sa.String(length=16), nullable=False),
        sa.Column("last_trade_ts_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_trade_id", sa.String(length=128), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_backfill_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["exchange_credentials.id"]),
        sa.CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_exchange_trade_sync_state_market_type",
        ),
        sa.CheckConstraint(
            "error_count >= 0",
            name="ck_exchange_trade_sync_state_error_count_min",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "symbol",
            "market_type",
            name="uq_exchange_trade_sync_state_scope",
        ),
    )
    op.create_index(
        "ix_exchange_trade_sync_state_user_id",
        "exchange_trade_sync_state",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_sync_state_account_id",
        "exchange_trade_sync_state",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_trade_sync_state_symbol",
        "exchange_trade_sync_state",
        ["symbol"],
        unique=False,
    )

    op.create_table(
        "exchange_order_metadata",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("exchange_name", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=128), nullable=True),
        sa.Column("client_order_id", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("config_id", sa.Integer(), nullable=True),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("history_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["exchange_credentials.id"]),
        sa.ForeignKeyConstraint(["config_id"], ["auto_trade_configs.id"]),
        sa.ForeignKeyConstraint(["position_id"], ["auto_trade_positions.id"]),
        sa.ForeignKeyConstraint(["history_id"], ["personal_analysis_history.id"]),
        sa.CheckConstraint(
            "source IN ('auto_trade_open', 'auto_trade_close', 'manual', 'unknown')",
            name="ck_exchange_order_metadata_source",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_exchange_order_metadata_user_id",
        "exchange_order_metadata",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_order_metadata_account_id",
        "exchange_order_metadata",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_order_metadata_config_id",
        "exchange_order_metadata",
        ["config_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_order_metadata_position_id",
        "exchange_order_metadata",
        ["position_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_order_metadata_history_id",
        "exchange_order_metadata",
        ["history_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_order_metadata_account_exchange_order",
        "exchange_order_metadata",
        ["account_id", "exchange_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_exchange_order_metadata_account_client_order",
        "exchange_order_metadata",
        ["account_id", "client_order_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_exchange_order_metadata_account_client_order",
        table_name="exchange_order_metadata",
    )
    op.drop_index(
        "ix_exchange_order_metadata_account_exchange_order",
        table_name="exchange_order_metadata",
    )
    op.drop_index("ix_exchange_order_metadata_history_id", table_name="exchange_order_metadata")
    op.drop_index("ix_exchange_order_metadata_position_id", table_name="exchange_order_metadata")
    op.drop_index("ix_exchange_order_metadata_config_id", table_name="exchange_order_metadata")
    op.drop_index("ix_exchange_order_metadata_account_id", table_name="exchange_order_metadata")
    op.drop_index("ix_exchange_order_metadata_user_id", table_name="exchange_order_metadata")
    op.drop_table("exchange_order_metadata")

    op.drop_index("ix_exchange_trade_sync_state_symbol", table_name="exchange_trade_sync_state")
    op.drop_index("ix_exchange_trade_sync_state_account_id", table_name="exchange_trade_sync_state")
    op.drop_index("ix_exchange_trade_sync_state_user_id", table_name="exchange_trade_sync_state")
    op.drop_table("exchange_trade_sync_state")

    op.drop_index(
        "ix_exchange_trade_ledger_position_traded_at",
        table_name="exchange_trade_ledger",
    )
    op.drop_index(
        "ix_exchange_trade_ledger_account_client_order_id",
        table_name="exchange_trade_ledger",
    )
    op.drop_index(
        "ix_exchange_trade_ledger_account_exchange_order_id",
        table_name="exchange_trade_ledger",
    )
    op.drop_index(
        "ix_exchange_trade_ledger_account_symbol_traded_at",
        table_name="exchange_trade_ledger",
    )
    op.drop_index("ix_exchange_trade_ledger_close_history_id", table_name="exchange_trade_ledger")
    op.drop_index("ix_exchange_trade_ledger_open_history_id", table_name="exchange_trade_ledger")
    op.drop_index(
        "ix_exchange_trade_ledger_auto_trade_position_id",
        table_name="exchange_trade_ledger",
    )
    op.drop_index(
        "ix_exchange_trade_ledger_auto_trade_config_id",
        table_name="exchange_trade_ledger",
    )
    op.drop_index("ix_exchange_trade_ledger_symbol", table_name="exchange_trade_ledger")
    op.drop_index("ix_exchange_trade_ledger_account_id", table_name="exchange_trade_ledger")
    op.drop_index("ix_exchange_trade_ledger_user_id", table_name="exchange_trade_ledger")
    op.drop_table("exchange_trade_ledger")
