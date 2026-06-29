"""drop the unused ix_oa_shadow_outcomes_entered index

Revision ID: 20260629_0047
Revises: 20260629_0046
Create Date: 2026-06-29 14:00:00

The `entered` column is a non-authoritative debug hint (the outcomes endpoint is
authoritative via the live position join), so it is never used for filtering and
its index is dead weight. Drop it. The column itself stays.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260629_0047"
down_revision: str | None = "20260629_0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "oa_shadow_outcomes"
_INDEX = "ix_oa_shadow_outcomes_entered"


def upgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)


def downgrade() -> None:
    op.create_index(_INDEX, _TABLE, ["entered"], unique=False)
