"""add live paper mode tables

Revision ID: 20260225_0005
Revises: 20260221_0004
Create Date: 2026-02-25 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260225_0005"
down_revision: str | None = "20260221_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_paper_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=False),
        sa.Column("strategy_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_running", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("total_balance_usdt", sa.Float(), nullable=False, server_default="1000"),
        sa.Column("per_trade_usdt", sa.Float(), nullable=False, server_default="100"),
        sa.Column("last_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_live_paper_profiles_user_id"),
    )
    op.create_index(
        "ix_live_paper_profiles_user_id",
        "live_paper_profiles",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_live_paper_profiles_strategy_id",
        "live_paper_profiles",
        ["strategy_id"],
        unique=False,
    )

    op.create_table(
        "live_paper_trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=False),
        sa.Column("strategy_revision", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=False),
        sa.Column("pnl_usdt", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="closed"),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
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
        sa.ForeignKeyConstraint(["profile_id"], ["live_paper_profiles.id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "strategy_revision",
            "entry_time",
            "exit_time",
            "side",
            "entry_price",
            name="uq_live_paper_trades_dedupe",
        ),
    )
    op.create_index(
        "ix_live_paper_trades_profile_id",
        "live_paper_trades",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_live_paper_trades_strategy_id",
        "live_paper_trades",
        ["strategy_id"],
        unique=False,
    )
    op.create_index(
        "ix_live_paper_trades_strategy_revision",
        "live_paper_trades",
        ["strategy_revision"],
        unique=False,
    )
    op.create_index(
        "ix_live_paper_trades_entry_time",
        "live_paper_trades",
        ["entry_time"],
        unique=False,
    )
    op.create_index(
        "ix_live_paper_trades_exit_time",
        "live_paper_trades",
        ["exit_time"],
        unique=False,
    )

    op.create_table(
        "live_paper_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("strategy_revision", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["profile_id"], ["live_paper_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_live_paper_events_profile_id",
        "live_paper_events",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_live_paper_events_strategy_revision",
        "live_paper_events",
        ["strategy_revision"],
        unique=False,
    )
    op.create_index(
        "ix_live_paper_events_event_time",
        "live_paper_events",
        ["event_time"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_live_paper_events_event_time", table_name="live_paper_events")
    op.drop_index("ix_live_paper_events_strategy_revision", table_name="live_paper_events")
    op.drop_index("ix_live_paper_events_profile_id", table_name="live_paper_events")
    op.drop_table("live_paper_events")

    op.drop_index("ix_live_paper_trades_exit_time", table_name="live_paper_trades")
    op.drop_index("ix_live_paper_trades_entry_time", table_name="live_paper_trades")
    op.drop_index("ix_live_paper_trades_strategy_revision", table_name="live_paper_trades")
    op.drop_index("ix_live_paper_trades_strategy_id", table_name="live_paper_trades")
    op.drop_index("ix_live_paper_trades_profile_id", table_name="live_paper_trades")
    op.drop_table("live_paper_trades")

    op.drop_index("ix_live_paper_profiles_strategy_id", table_name="live_paper_profiles")
    op.drop_index("ix_live_paper_profiles_user_id", table_name="live_paper_profiles")
    op.drop_table("live_paper_profiles")
