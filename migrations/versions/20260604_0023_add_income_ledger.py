"""add exchange_income_ledger for funding/income sync

Revision ID: 20260604_0023
Revises: 20260604_0022
Create Date: 2026-06-04 00:30:00

W9 PnL accuracy — funding. Binance ``/fapi/v1/income`` is the authoritative
source for funding fees (paid/received every 8h), which the platform did not
track. This table mirrors income rows (FUNDING_FEE for now); ``tranId`` is
unique per (user, income type) so the idempotency key is
(account_id, exchange_name, income_type, tran_id).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260604_0023"
down_revision: str | None = "20260604_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "exchange_income_ledger"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE in _table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("exchange_credentials.id"),
            nullable=False,
        ),
        sa.Column("exchange_name", sa.String(length=32), nullable=False),
        sa.Column("market_type", sa.String(length=16), nullable=False, server_default="futures"),
        sa.Column("income_type", sa.String(length=32), nullable=False),
        sa.Column("asset", sa.String(length=32), nullable=False),
        sa.Column("income", sa.Float(), nullable=False, server_default="0"),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("tran_id", sa.String(length=64), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column("info", sa.String(length=64), nullable=True),
        sa.Column("income_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_exchange_income_ledger_market_type",
        ),
        sa.UniqueConstraint(
            "account_id",
            "exchange_name",
            "income_type",
            "tran_id",
            name="uq_exchange_income_ledger_identity",
        ),
    )
    op.create_index(
        "ix_exchange_income_ledger_user_id", _TABLE, ["user_id"]
    )
    op.create_index(
        "ix_exchange_income_ledger_account_id", _TABLE, ["account_id"]
    )
    op.create_index(
        "ix_exchange_income_ledger_symbol", _TABLE, ["symbol"]
    )
    op.create_index(
        "ix_exchange_income_ledger_account_symbol_income_at",
        _TABLE,
        ["account_id", "symbol", "income_at"],
    )
    op.create_index(
        "ix_exchange_income_ledger_account_type_income_at",
        _TABLE,
        ["account_id", "income_type", "income_at"],
    )


def downgrade() -> None:
    if _TABLE in _table_names():
        op.drop_table(_TABLE)
