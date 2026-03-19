"""update auto trade risk mode constraint

Revision ID: 20260310_0008
Revises: 20260308_0007
Create Date: 2026-03-10 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260310_0008"
down_revision: str | None = "20260308_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.drop_constraint("ck_auto_trade_cfg_risk_mode", type_="check")
        batch_op.alter_column(
            "risk_mode",
            existing_type=sa.String(length=8),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_auto_trade_cfg_risk_mode",
            "risk_mode LIKE '1:%'",
        )


def downgrade() -> None:
    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.drop_constraint("ck_auto_trade_cfg_risk_mode", type_="check")
        batch_op.alter_column(
            "risk_mode",
            existing_type=sa.String(length=16),
            type_=sa.String(length=8),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_auto_trade_cfg_risk_mode",
            "risk_mode IN ('1:2', '1:3')",
        )
