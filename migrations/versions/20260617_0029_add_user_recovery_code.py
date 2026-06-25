"""add user_recovery_code (2FA one-time recovery codes)

Revision ID: 20260617_0029
Revises: 20260617_0028
Create Date: 2026-06-17 13:00:00

One-time 2FA recovery codes, stored hashed. Consumed on first use (``used_at``),
regenerated on re-enroll, cleared on disable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260617_0029"
down_revision: str | None = "20260617_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_recovery_code"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE in _table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("user_id", "code_hash", name="uq_user_recovery_code"),
    )
    op.create_index("ix_user_recovery_code_user_id", _TABLE, ["user_id"])
    op.create_index("ix_user_recovery_code_code_hash", _TABLE, ["code_hash"])


def downgrade() -> None:
    if _TABLE in _table_names():
        op.drop_table(_TABLE)
