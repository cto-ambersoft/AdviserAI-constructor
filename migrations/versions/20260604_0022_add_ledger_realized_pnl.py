"""add exchange_trade_ledger.realized_pnl + backfill from raw_trade

Revision ID: 20260604_0022
Revises: 20260603_0021
Create Date: 2026-06-04 00:00:00

W9 PnL accuracy — authoritative realized PnL. Binance USDⓈ-M ``userTrades``
already carries a per-fill ``realizedPnl`` (gross price PnL, excluding
commission/funding) which the ledger stored only inside ``raw_trade`` JSON. This
adds a first-class nullable ``realized_pnl`` column and backfills it from the
already-synced ``raw_trade`` so existing rows become authoritative without
re-fetching from the exchange. Nullable so legacy/external fills without the
field stay NULL (the PnL engine falls back to FIFO for those).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

from app.services.auto_trade.trade_sync import extract_realized_pnl

# revision identifiers, used by Alembic.
revision: str = "20260604_0022"
down_revision: str | None = "20260603_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "exchange_trade_ledger"
_COLUMN = "realized_pnl"


def _has_column(bind: Connection) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def backfill_realized_pnl(bind: Connection) -> int:
    """Populate ``realized_pnl`` from each row's stored ``raw_trade`` JSON.

    Portable across Postgres (JSON → dict) and SQLite (JSON → text): the raw
    value is normalised to a dict before extraction. Only rows that yield a
    numeric ``realizedPnl`` (including an authoritative ``0.0``) are updated;
    rows missing the field stay NULL. Returns the number of rows updated.
    """
    rows = bind.execute(
        sa.text(f"SELECT id, raw_trade FROM {_TABLE} WHERE {_COLUMN} IS NULL")
    ).fetchall()
    updated = 0
    for row_id, raw in rows:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        value = extract_realized_pnl(raw)
        if value is None:
            continue
        bind.execute(
            sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = :value WHERE id = :id"),
            {"value": value, "id": row_id},
        )
        updated += 1
    return updated


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Float(), nullable=True))
    backfill_realized_pnl(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
