"""add upper-bound CHECK constraints to auto_trade_risk_configs

Revision ID: 20260603_0020
Revises: 20260527_0019
Create Date: 2026-06-03 00:00:00

W8 follow-up (code review I4). The 0018 table only enforced lower bounds, so a
non-Pydantic writer could persist e.g. ``leverage_ceiling = 10000`` or
``daily_loss_limit_pct = 5000`` — values the engine would faithfully enforce as
effectively no limit. These CHECKs make the DB mirror the API schema's upper
bounds (``leverage_ceiling <= 125``, ``daily_loss_limit_pct <= 100``).

Batch mode keeps it portable: native ``ALTER TABLE ADD CONSTRAINT`` on
PostgreSQL, table-recreate on SQLite.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260603_0020"
down_revision: str | None = "20260527_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("auto_trade_risk_configs") as batch_op:
        batch_op.create_check_constraint(
            "ck_at_risk_leverage_max",
            "leverage_ceiling IS NULL OR leverage_ceiling <= 125",
        )
        batch_op.create_check_constraint(
            "ck_at_risk_daily_loss_pct_max",
            "daily_loss_limit_pct IS NULL OR daily_loss_limit_pct <= 100",
        )


def downgrade() -> None:
    with op.batch_alter_table("auto_trade_risk_configs") as batch_op:
        batch_op.drop_constraint("ck_at_risk_daily_loss_pct_max", type_="check")
        batch_op.drop_constraint("ck_at_risk_leverage_max", type_="check")
