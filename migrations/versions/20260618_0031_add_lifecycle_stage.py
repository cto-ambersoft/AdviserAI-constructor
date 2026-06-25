"""add lifecycle_stage to auto_trade_configs (B5 — W10 Strategy Promotion)

Revision ID: 20260618_0031
Revises: 20260617_0030
Create Date: 2026-06-18 10:00:00

Phase 4 of Milestone 4 — Strategy Promotion Pipeline (W10).

Adds the lifecycle stage a strategy config sits in (research → sandbox →
validation → live, plus terminal rejected/archived), driven by the promotion
FSM (``app/services/auto_trade/promotion/state_machine.py``).

``server_default='live'`` backfills every pre-existing row to ``live`` — a
zero-behavior-change migration: the stage is descriptive only until the sandbox
paper-execution gate (P4-4) and KPI Gate (P4-2) land. The CHECK mirrors the
FSM's stage set so a bad value is a hard error at write time.

Batch mode keeps it portable (native ALTER on PostgreSQL, recreate on SQLite,
triggered by the CHECK).

Ops note: ``create_check_constraint`` validates all existing rows under an
ACCESS EXCLUSIVE lock. ``auto_trade_configs`` is small today so this is instant;
if it ever grows large, switch to ``ADD CONSTRAINT ... NOT VALID`` followed by a
separate ``VALIDATE CONSTRAINT`` to avoid a long write-blocking lock.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260618_0031"
down_revision: str | None = "20260617_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_configs"
_CHECK = "ck_auto_trade_cfg_lifecycle_stage"
_CHECK_CONDITION = (
    "lifecycle_stage IN "
    "('research', 'sandbox', 'validation', 'live', 'rejected', 'archived')"
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_stage",
                sa.String(length=16),
                nullable=False,
                server_default="live",
            )
        )
        batch_op.create_check_constraint(_CHECK, _CHECK_CONDITION)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.drop_column("lifecycle_stage")
