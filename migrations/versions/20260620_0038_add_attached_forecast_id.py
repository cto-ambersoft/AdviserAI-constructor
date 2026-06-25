"""add attached_forecast_id to auto_trade_configs (T16 — W12e/AC#1)

Revision ID: 20260620_0038
Revises: 20260620_0037
Create Date: 2026-06-20 14:00:00

M4 remediation T16 (audit W12e). Lets a live auto-trade strategy be attached to a
catalogue forecast — a provenance link to the core ``ai_forecast_catalogue.forecastId``.
Nullable; existing rows stay unattached. Indexed for "strategies using forecast X".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260620_0038"
down_revision: str | None = "20260620_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_configs"
_COLUMN = "attached_forecast_id"
_INDEX = "ix_auto_trade_configs_attached_forecast_id"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column(_COLUMN, sa.String(length=64), nullable=True))
        batch_op.create_index(_INDEX, [_COLUMN])


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_index(_INDEX)
        batch_op.drop_column(_COLUMN)
