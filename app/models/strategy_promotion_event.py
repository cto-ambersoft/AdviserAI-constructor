from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class StrategyPromotionEvent(Base, TimestampMixin):
    """First-class audit trail of strategy lifecycle transitions (T19 / W10f).

    One row per promote / demote / gate-failure, with the stage transition, the
    decision, a snapshot of the KPI-gate criteria, and the actor. Complements the
    ``auto_trade_events`` stream (which is for notifications/SSE) with a queryable,
    durable lifecycle history.
    """

    __tablename__ = "strategy_promotion_events"
    __table_args__ = (
        Index("ix_strategy_promotion_events_config_created", "config_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("auto_trade_configs.id"), nullable=False, index=True
    )
    from_stage: Mapped[str] = mapped_column(String(16), nullable=False)
    to_stage: Mapped[str] = mapped_column(String(16), nullable=False)
    # 'promoted' | 'demoted' | 'gate_failed'
    decision: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    # Snapshot of the KPI-gate criteria (pass/fail) at decision time; NULL for demote.
    kpi_snapshot_json: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    # Who/what triggered it: 'user' (step-up promote/demote), 'anomaly', 'cron', ...
    actor: Mapped[str | None] = mapped_column(String(24), nullable=True)
