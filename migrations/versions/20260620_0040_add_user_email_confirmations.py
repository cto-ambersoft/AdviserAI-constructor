"""add user_email_confirmations table (T20 — W11c email confirmation)

Revision ID: 20260620_0040
Revises: 20260620_0039
Create Date: 2026-06-20 16:00:00

M4 remediation T20 (audit W11c). One-time email confirmation codes for critical
actions (via Resend) — a second factor alongside step-up. Codes are stored hashed,
single-use (``consumed_at``), TTL-bounded (``expires_at``). Purely additive:
accounts that never use it are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260620_0040"
down_revision: str | None = "20260620_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_email_confirmations"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{_TABLE}_user_id", _TABLE, ["user_id"])
    op.create_index(f"ix_{_TABLE}_action", _TABLE, ["action"])
    op.create_index(f"ix_{_TABLE}_code_hash", _TABLE, ["code_hash"])


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_code_hash", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_action", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
