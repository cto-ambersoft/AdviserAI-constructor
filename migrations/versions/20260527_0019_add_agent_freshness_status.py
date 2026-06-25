"""add agent_freshness_status for the 4h data-freshness sweep

Revision ID: 20260527_0019
Revises: 20260526_0018
Create Date: 2026-05-27 00:00:00

W8 of Milestone 4 — Data Freshness.

Adds ``agent_freshness_status``: the current freshness snapshot per analysis
profile and AI agent, upserted on ``(profile_id, agent_key)`` by the scheduled
4h sweep. ``last_data_at`` / ``age_minutes`` are nullable so a profile/agent
with no underlying data yet (``is_fresh = False``) is representable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260527_0019"
down_revision: str | None = "20260526_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _get_table_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def upgrade() -> None:
    if "agent_freshness_status" in _get_table_names():
        return
    op.create_table(
        "agent_freshness_status",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "profile_id",
            sa.Integer(),
            sa.ForeignKey("personal_analysis_profiles.id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("agent_key", sa.String(length=16), nullable=False),
        sa.Column("last_data_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("age_minutes", sa.Integer(), nullable=True),
        sa.Column("is_fresh", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("profile_id", "agent_key", name="uq_agent_freshness_profile_agent"),
    )
    op.create_index(
        "ix_agent_freshness_status_profile_id", "agent_freshness_status", ["profile_id"]
    )
    op.create_index("ix_agent_freshness_status_symbol", "agent_freshness_status", ["symbol"])
    op.create_index("ix_agent_freshness_status_agent_key", "agent_freshness_status", ["agent_key"])
    op.create_index(
        "ix_agent_freshness_status_checked_at", "agent_freshness_status", ["checked_at"]
    )


def downgrade() -> None:
    if "agent_freshness_status" in _get_table_names():
        op.drop_table("agent_freshness_status")
