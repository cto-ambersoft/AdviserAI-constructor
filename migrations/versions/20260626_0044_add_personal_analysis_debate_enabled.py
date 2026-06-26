"""add personal_analysis_profiles.debate_enabled

Revision ID: 20260626_0044
Revises: 20260625_0043
Create Date: 2026-06-26 12:00:00

Debate integration: opt-in adversarial review of the personal-analysis forecast.
Purely additive — a nullable boolean (NULL/False = disabled). Existing profiles
default to debate off, so no backfill is needed. Portable via batch mode
(native ALTER on PostgreSQL, recreate on SQLite).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260626_0044"
down_revision: str | None = "20260625_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "personal_analysis_profiles"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("debate_enabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("debate_enabled")
