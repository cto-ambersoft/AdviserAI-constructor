"""add decision_event_id to auto_trade_positions

Revision ID: 20260513_0016
Revises: 20260512_0015
Create Date: 2026-05-13 00:00:00

W2 of Milestone 4 — last-mile ai_trend traceability. Adds a nullable
``decision_event_id`` column on ``auto_trade_positions`` so each position
opened under an active AI overlay carries a stable pointer back to the
exact AI decision document in core's ``ai_decision_events`` collection.

No FK constraint is declared because the referenced id lives in MongoDB,
not Postgres. An index is created for future audit / admin joins.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260513_0016"
down_revision: str | None = "20260512_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _get_column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _get_index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if "decision_event_id" not in _get_column_names("auto_trade_positions"):
        with op.batch_alter_table("auto_trade_positions") as batch_op:
            batch_op.add_column(
                sa.Column("decision_event_id", sa.String(length=36), nullable=True)
            )

    if "ix_auto_trade_positions_decision_event_id" not in _get_index_names(
        "auto_trade_positions"
    ):
        op.create_index(
            "ix_auto_trade_positions_decision_event_id",
            "auto_trade_positions",
            ["decision_event_id"],
        )


def downgrade() -> None:
    if "ix_auto_trade_positions_decision_event_id" in _get_index_names(
        "auto_trade_positions"
    ):
        op.drop_index(
            "ix_auto_trade_positions_decision_event_id",
            table_name="auto_trade_positions",
        )
    if "decision_event_id" in _get_column_names("auto_trade_positions"):
        with op.batch_alter_table("auto_trade_positions") as batch_op:
            batch_op.drop_column("decision_event_id")
