from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class StrategyHealthSnapshot(Base, TimestampMixin):
    """One persisted Strategy Health Score reading for a live strategy (W9 — T0.2).

    The on-read ``compute_strategy_health`` (W8) gives a strategy's health *now*;
    the KPI-Guard (W9) needs *recent history* to decide whether to auto-pause, and
    the AC#7 dashboard renders a trend. This is an **append-only time series** — the
    KPI-Guard cron + the on-close fast path each append a new row stamped with the
    snapshot's ``computed_at``. There is intentionally **no unique key**, so repeated
    or concurrent writes can never collide on a constraint (the W8 freshness-upsert
    race, I7, does not apply to an append-only series). "Latest per config" is the
    ``(config_id, computed_at)`` composite index, ordered descending.
    """

    __tablename__ = "strategy_health_snapshots"
    __table_args__ = (
        Index(
            "ix_strategy_health_snapshots_config_computed",
            "config_id",
            "computed_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("auto_trade_configs.id"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    window_days: Mapped[int] = mapped_column(Integer(), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    win_rate_pct: Mapped[float] = mapped_column(Float(), nullable=False)
    max_dd_pct: Mapped[float] = mapped_column(Float(), nullable=False)
    total_pnl_usdt: Mapped[float] = mapped_column(Float(), nullable=False)
    roi_pct: Mapped[float] = mapped_column(Float(), nullable=False)
    sharpe_proxy: Mapped[float] = mapped_column(Float(), nullable=False)
    stability_score: Mapped[float] = mapped_column(Float(), nullable=False)
    health_score: Mapped[float] = mapped_column(Float(), nullable=False)
    health_class: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Forward-compat envelope for KPI fields added after the columns are frozen.
    payload: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
