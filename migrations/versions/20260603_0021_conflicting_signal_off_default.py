"""make conflicting_signal_policy 'off' a valid value and the default

Revision ID: 20260603_0021
Revises: 20260603_0020
Create Date: 2026-06-03 01:00:00

W8 follow-up (code review I5). Conflict blocking should be opt-in: setting any
other risk limit must not silently start blocking opposite signals. Adds 'off'
to the policy CHECK and flips the column default from 'block_opposite' to 'off'.
Batch mode keeps it portable (native ALTER on PostgreSQL, recreate on SQLite).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260603_0021"
down_revision: str | None = "20260603_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("auto_trade_risk_configs") as batch_op:
        batch_op.drop_constraint("ck_at_risk_conflicting_policy", type_="check")
        batch_op.create_check_constraint(
            "ck_at_risk_conflicting_policy",
            "conflicting_signal_policy IN ('off', 'block_opposite', 'net', 'replace')",
        )
        batch_op.alter_column("conflicting_signal_policy", server_default="off")


def downgrade() -> None:
    with op.batch_alter_table("auto_trade_risk_configs") as batch_op:
        batch_op.alter_column("conflicting_signal_policy", server_default="block_opposite")
        batch_op.drop_constraint("ck_at_risk_conflicting_policy", type_="check")
        batch_op.create_check_constraint(
            "ck_at_risk_conflicting_policy",
            "conflicting_signal_policy IN ('block_opposite', 'net', 'replace')",
        )
