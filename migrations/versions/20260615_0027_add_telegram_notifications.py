"""add telegram notification settings + delivery ledger

Revision ID: 20260615_0027
Revises: 20260605_0026
Create Date: 2026-06-15 12:00:00

Telegram trade notifications (phase 1). Two tables:

* ``telegram_notification_settings`` — one row per user holding the linked
  ``chat_id``, the master ``enabled`` switch, per-family toggles, and the
  one-time deep-link code used during the webhook ``/start`` handshake.
* ``telegram_notification_deliveries`` — idempotency + retry ledger keyed by
  ``event_id`` (each ``auto_trade_events`` row yields at most one notification),
  so the dispatcher never double-sends and can retry ``failed`` rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260615_0027"
down_revision: str | None = "20260605_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTINGS = "telegram_notification_settings"
_DELIVERIES = "telegram_notification_deliveries"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _table_names()
    if _SETTINGS not in existing:
        op.create_table(
            _SETTINGS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("chat_id", sa.BigInteger(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("notify_on_open", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("notify_on_close", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("notify_on_risk", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("link_code", sa.String(length=32), nullable=True),
            sa.Column("link_code_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
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
        # ``unique=True, index=True`` on the ORM column maps to a single unique
        # index named after the column — mirror that here (not a separate
        # UniqueConstraint) so the migration matches the model exactly.
        op.create_index(
            "ix_telegram_notification_settings_user_id",
            _SETTINGS,
            ["user_id"],
            unique=True,
        )
        op.create_index(
            "ix_telegram_notification_settings_link_code", _SETTINGS, ["link_code"]
        )

    if _DELIVERIES not in existing:
        op.create_table(
            _DELIVERIES,
            sa.Column(
                "event_id",
                sa.Integer(),
                sa.ForeignKey("auto_trade_events.id"),
                primary_key=True,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
        op.create_index(
            "ix_telegram_notification_deliveries_user_id", _DELIVERIES, ["user_id"]
        )


def downgrade() -> None:
    existing = _table_names()
    if _DELIVERIES in existing:
        op.drop_table(_DELIVERIES)
    if _SETTINGS in existing:
        op.drop_table(_SETTINGS)
