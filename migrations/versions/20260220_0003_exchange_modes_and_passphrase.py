"""exchange modes and passphrase

Revision ID: 20260220_0003
Revises: 20260220_0002
Create Date: 2026-02-20 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260220_0003"
down_revision: str | None = "20260220_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exchange_credentials",
        sa.Column("mode", sa.String(length=8), nullable=False, server_default="real"),
    )
    op.add_column(
        "exchange_credentials",
        sa.Column("encrypted_passphrase", sa.String(length=1024), nullable=True),
    )
    op.create_check_constraint(
        "ck_exchange_credentials_mode",
        "exchange_credentials",
        "mode IN ('demo', 'real')",
    )
    op.create_unique_constraint(
        "uq_exchange_user_name_label",
        "exchange_credentials",
        ["user_id", "exchange_name", "account_label"],
    )
    op.alter_column("exchange_credentials", "mode", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_exchange_user_name_label", "exchange_credentials", type_="unique")
    op.drop_constraint("ck_exchange_credentials_mode", "exchange_credentials", type_="check")
    op.drop_column("exchange_credentials", "encrypted_passphrase")
    op.drop_column("exchange_credentials", "mode")
