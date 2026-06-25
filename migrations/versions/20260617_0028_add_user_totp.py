"""add user_totp (2FA TOTP enrollment)

Revision ID: 20260617_0028
Revises: 20260615_0027
Create Date: 2026-06-17 12:00:00

One row per user holding the Fernet-encrypted TOTP secret and ``confirmed_at`` —
the enrollment is only active (2FA enabled) once a valid code has confirmed it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260617_0028"
down_revision: str | None = "20260615_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_totp"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE in _table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("secret_encrypted", sa.String(length=255), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    # ``unique=True, index=True`` on the ORM column → one unique index named after
    # the column (matches the model exactly).
    op.create_index("ix_user_totp_user_id", _TABLE, ["user_id"], unique=True)


def downgrade() -> None:
    if _TABLE in _table_names():
        op.drop_table(_TABLE)
