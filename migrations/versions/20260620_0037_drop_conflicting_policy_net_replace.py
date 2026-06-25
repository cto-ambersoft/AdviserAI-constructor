"""drop interface-only net/replace from conflicting_signal_policy CHECK (T13 — W8c)

Revision ID: 20260620_0037
Revises: 20260620_0036
Create Date: 2026-06-20 13:00:00

M4 remediation T13 (audit W8c). ``net`` and ``replace`` were selectable in the
API/DB but never enforced by the pre-trade engine (logged and treated as allow).
They are removed so the schema/UI can't offer a silently-ignored option.

Backfill: any existing row holding the dropped values is migrated to the safer
``block_opposite`` before the tightened CHECK is applied. The CHECK is recreated
via batch mode (native on PostgreSQL, recreate on SQLite).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260620_0037"
down_revision: str | None = "20260620_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_risk_configs"
_CHECK = "ck_at_risk_conflicting_policy"
_NEW_CONDITION = "conflicting_signal_policy IN ('off', 'block_opposite')"
_OLD_CONDITION = "conflicting_signal_policy IN ('off', 'block_opposite', 'net', 'replace')"


def upgrade() -> None:
    op.execute(
        "UPDATE auto_trade_risk_configs SET conflicting_signal_policy = 'block_opposite' "
        "WHERE conflicting_signal_policy IN ('net', 'replace')"
    )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, _NEW_CONDITION)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, _OLD_CONDITION)
