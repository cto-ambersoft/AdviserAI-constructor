"""add composite index on auto_trade_events for sweep dedup (P4-3/P4-6, review S4)

Revision ID: 20260618_0035
Revises: 20260618_0034
Create Date: 2026-06-18 12:00:00

The anomaly sweep (B6) and promotion-gate sweep (B5) dedup by counting recent
events: ``WHERE config_id=? AND event_type=? AND created_at >= ?``. The table
indexes config_id and event_type singly; this composite index covers the whole
predicate so the dedup count stays cheap as the event log grows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260618_0035"
down_revision: str | None = "20260618_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_events"
_INDEX = "ix_auto_trade_events_config_type_created"


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, ["config_id", "event_type", "created_at"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
