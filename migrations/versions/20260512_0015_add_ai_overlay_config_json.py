"""add ai_overlay_config_json to auto_trade_configs

Revision ID: 20260512_0015
Revises: 20260511_0014
Create Date: 2026-05-12 00:00:00

W4 of Milestone 4 — Dynamic Parameter Adjustment based on ai_trend.

Adds the ``ai_overlay_config_json`` column on ``auto_trade_configs`` which
stores the per-user opt-in overlay that scales ATR multiplier / RSI
thresholds and blocks opposite-side entries using the freshest ai_trend
record from ``personal_analysis_history``. The column is nullable; a NULL
value is interpreted as "overlay disabled" so the migration is a no-op
for existing rows and preserves current behaviour.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260512_0015"
down_revision: str | None = "20260511_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _get_column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "ai_overlay_config_json" in _get_column_names("auto_trade_configs"):
        return

    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.add_column(sa.Column("ai_overlay_config_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "ai_overlay_config_json" not in _get_column_names("auto_trade_configs"):
        return

    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.drop_column("ai_overlay_config_json")
