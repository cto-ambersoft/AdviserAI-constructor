"""add auto_trade_config_revisions table (append-only config history — audit §7)

Revision ID: 20260625_0043
Revises: 20260625_0042
Create Date: 2026-06-25 13:00:00

Append-only revision history of a strategy's editable config content: one
immutable row per content change (``revision_number`` + ``content_hash`` +
full ``snapshot_json``), enabling change auditing and rollback to a prior
revision. Purely additive; existing configs are unaffected (their first
revision is recorded on the next edit).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260625_0043"
down_revision: str | None = "20260625_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_config_revisions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=24), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["config_id"], ["auto_trade_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{_TABLE}_config_id", _TABLE, ["config_id"])
    op.create_index(
        f"ix_{_TABLE}_config_number", _TABLE, ["config_id", "revision_number"]
    )


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_config_number", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_config_id", table_name=_TABLE)
    op.drop_table(_TABLE)
