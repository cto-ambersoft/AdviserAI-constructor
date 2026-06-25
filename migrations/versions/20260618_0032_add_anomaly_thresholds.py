"""add Strategy anomaly-detection thresholds to auto_trade_risk_configs (B6 — W12)

Revision ID: 20260618_0032
Revises: 20260618_0031
Create Date: 2026-06-18 10:30:00

Phase 4 of Milestone 4 — Strategy Anomaly Detection (W12).

Extends the risk-config satellite with the anomaly detector's controls
(``app/services/auto_trade/anomaly/detector.py``). ``anomaly_detection_enabled``
is the master switch — default ``false`` (fully opt-in, like kpi_guard /
kill_switch): the detector only alerts, and ships off until thresholds are
calibrated on real series. The params are nullable (``NULL`` ⇒ the detector's
engine default: z-threshold 3.0, window 20). Bound CHECKs mirror the API schema.

Batch mode keeps it portable (native ALTER on PostgreSQL, recreate on SQLite).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260618_0032"
down_revision: str | None = "20260618_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auto_trade_risk_configs"

_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_at_risk_anomaly_z_threshold_pos",
        "anomaly_z_threshold IS NULL OR (anomaly_z_threshold > 0 AND anomaly_z_threshold <= 20)",
    ),
    (
        "ck_at_risk_anomaly_window_min",
        "anomaly_window IS NULL OR (anomaly_window >= 2 AND anomaly_window <= 1000)",
    ),
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "anomaly_detection_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("anomaly_z_threshold", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("anomaly_window", sa.Integer(), nullable=True))
        for name, condition in _CHECKS:
            batch_op.create_check_constraint(name, condition)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for name, _ in reversed(_CHECKS):
            batch_op.drop_constraint(name, type_="check")
        batch_op.drop_column("anomaly_window")
        batch_op.drop_column("anomaly_z_threshold")
        batch_op.drop_column("anomaly_detection_enabled")
