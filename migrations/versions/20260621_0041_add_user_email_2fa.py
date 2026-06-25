"""add user_email_2fa table (email confirmation as a full second factor)

Revision ID: 20260621_0041
Revises: 20260620_0040
Create Date: 2026-06-21 12:00:00

Email-2FA enrollment, mirroring ``user_totp``: a per-user, opt-in second factor that
is active only once the user verifies a code emailed to the account address
(``confirmed_at``). Holds the per-factor brute-force lockout (``failed_attempts`` /
``locked_until``). Purely additive: accounts that never enroll are unaffected, and
TOTP-only users are untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260621_0041"
down_revision: str | None = "20260620_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_email_2fa"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
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
    # ``unique=True, index=True`` on the ORM column → one unique index named after
    # the column (matches the model exactly, mirroring user_totp).
    op.create_index(f"ix_{_TABLE}_user_id", _TABLE, ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
