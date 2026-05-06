"""add strategy profile json to auto trade configs

Revision ID: 20260407_0011
Revises: 20260313_0010
Create Date: 2026-04-07 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260407_0011"
down_revision: str | None = "20260313_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _get_column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "strategy_profile_json" in _get_column_names("auto_trade_configs"):
        return

    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.add_column(sa.Column("strategy_profile_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "strategy_profile_json" not in _get_column_names("auto_trade_configs"):
        return

    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.drop_column("strategy_profile_json")
