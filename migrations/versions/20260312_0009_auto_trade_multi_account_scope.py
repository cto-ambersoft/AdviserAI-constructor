"""scope auto trade storage by account/config

Revision ID: 20260312_0009
Revises: 20260310_0008
Create Date: 2026-03-12 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260312_0009"
down_revision: str | None = "20260310_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.drop_constraint("uq_auto_trade_configs_user_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_auto_trade_configs_user_account_id",
            ["user_id", "account_id"],
        )

    # Some environments were created with different index naming conventions.
    op.execute(sa.text("DROP INDEX IF EXISTS uq_auto_trade_positions_user_open"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_auto_trade_positions_user_open"))
    op.create_index(
        "uq_auto_trade_positions_user_account_open",
        "auto_trade_positions",
        ["user_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )

    with op.batch_alter_table("auto_trade_signal_state") as batch_op:
        batch_op.drop_constraint("uq_auto_trade_signal_state_user_id", type_="unique")

    with op.batch_alter_table("auto_trade_signal_queue") as batch_op:
        batch_op.drop_constraint("uq_auto_trade_signal_queue_history_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_auto_trade_signal_queue_history_config_id",
            ["history_id", "config_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("auto_trade_signal_queue") as batch_op:
        batch_op.drop_constraint("uq_auto_trade_signal_queue_history_config_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_auto_trade_signal_queue_history_id",
            ["history_id"],
        )

    with op.batch_alter_table("auto_trade_signal_state") as batch_op:
        batch_op.create_unique_constraint("uq_auto_trade_signal_state_user_id", ["user_id"])

    op.execute(sa.text("DROP INDEX IF EXISTS uq_auto_trade_positions_user_account_open"))
    op.create_index(
        "uq_auto_trade_positions_user_open",
        "auto_trade_positions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )

    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.drop_constraint("uq_auto_trade_configs_user_account_id", type_="unique")
        batch_op.create_unique_constraint("uq_auto_trade_configs_user_id", ["user_id"])
