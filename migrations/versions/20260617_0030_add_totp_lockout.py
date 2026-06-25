"""add 2FA brute-force lockout columns to user_totp

Revision ID: 20260617_0030
Revises: 20260617_0029
Create Date: 2026-06-17 14:00:00

``failed_attempts`` / ``locked_until`` implement per-enrollment lockout after too
many failed TOTP codes (C1) — a 6-digit code is otherwise online-guessable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260617_0030"
down_revision: str | None = "20260617_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_totp"


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    existing = _columns()
    if "failed_attempts" not in existing:
        op.add_column(
            _TABLE,
            sa.Column(
                "failed_attempts", sa.Integer(), nullable=False, server_default="0"
            ),
        )
    if "locked_until" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    existing = _columns()
    if "locked_until" in existing:
        op.drop_column(_TABLE, "locked_until")
    if "failed_attempts" in existing:
        op.drop_column(_TABLE, "failed_attempts")
