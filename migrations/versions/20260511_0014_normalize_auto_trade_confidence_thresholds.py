"""normalize auto-trade confidence thresholds to percent units

Revision ID: 20260511_0014
Revises: 20260504_0013
Create Date: 2026-05-11 00:00:00

Pre-existing ``auto_trade_configs`` rows could carry ``min_confidence_pct``
or ``fast_close_confidence_pct`` stored as fractions in (0, 1) instead of
percents in [1, 100] because the original schema only validated the range
[0, 100]. Once such a row landed in the DB, the entry-gate at
``service.py:_process_without_open_position`` silently let every signal
through (because every realistic signal lies in [0, 100] and therefore
exceeds e.g. 0.65). This migration:

  1. Backfills any fractional rows by multiplying them by 100. After the
     update every row is guaranteed to live in [1, 100].
  2. Tightens the check-constraints from ``>= 0`` to ``>= 1`` so the
     fractional form can never be re-introduced.

The schema-side validator on ``AutoTradeConfigUpsertRequest`` (and the
defensive normalization in ``parse_auto_trade_signal``) shipped in the
same change-set close the input side; this migration heals the data side.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260511_0014"
down_revision: str | None = "20260504_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Backfill fractional rows so the new constraint can be applied.
    op.execute(
        """
        UPDATE auto_trade_configs
        SET min_confidence_pct = min_confidence_pct * 100
        WHERE min_confidence_pct > 0 AND min_confidence_pct < 1
        """
    )
    op.execute(
        """
        UPDATE auto_trade_configs
        SET fast_close_confidence_pct = fast_close_confidence_pct * 100
        WHERE fast_close_confidence_pct > 0 AND fast_close_confidence_pct < 1
        """
    )

    # 2. Tighten the check-constraints. ``batch_alter_table`` keeps the
    # migration portable across PostgreSQL and SQLite (used by tests).
    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.drop_constraint(
            "ck_auto_trade_cfg_min_confidence_bounds",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_auto_trade_cfg_min_confidence_bounds",
            "min_confidence_pct >= 1 AND min_confidence_pct <= 100",
        )
        batch_op.drop_constraint(
            "ck_auto_trade_cfg_fast_close_confidence_bounds",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_auto_trade_cfg_fast_close_confidence_bounds",
            "fast_close_confidence_pct >= 1 AND fast_close_confidence_pct <= 100",
        )


def downgrade() -> None:
    # Restore the original (loose) bounds. We do NOT reverse the backfill —
    # multiplying back by 0.01 would silently re-break any threshold that
    # legitimately landed at e.g. 62.0 long before the bug surfaced.
    with op.batch_alter_table("auto_trade_configs") as batch_op:
        batch_op.drop_constraint(
            "ck_auto_trade_cfg_min_confidence_bounds",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_auto_trade_cfg_min_confidence_bounds",
            "min_confidence_pct >= 0 AND min_confidence_pct <= 100",
        )
        batch_op.drop_constraint(
            "ck_auto_trade_cfg_fast_close_confidence_bounds",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_auto_trade_cfg_fast_close_confidence_bounds",
            "fast_close_confidence_pct >= 0 AND fast_close_confidence_pct <= 100",
        )
