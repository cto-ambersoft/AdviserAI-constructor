from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class OaShadowOutcome(Base, TimestampMixin):
    """Shadow (counterfactual) outcome for an Outcome-Aware forecast.

    Defeats selection/censoring bias (D4): every personal-analysis forecast is
    recorded here with its predicted direction/confidence and a horizon. After
    the horizon closes, a Taskiq backfill fills ``realized_move_pct`` from OHLCV
    using point-in-time (backward as-of) lookups, so OA accuracy can later be
    computed over BOTH executed trades and the trades that were never entered.

    One row per forecast (``history_id`` unique). ``entered`` is a denormalized
    hint refreshed by the backfill; the outcomes endpoint stays authoritative by
    excluding forecasts that actually became positions.
    """

    __tablename__ = "oa_shadow_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("personal_analysis_profiles.id"),
        nullable=False,
        index=True,
    )
    history_id: Mapped[int] = mapped_column(
        ForeignKey("personal_analysis_history.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    # core ai_decision_events id (the join key the core accuracy loop uses).
    decision_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    signal_time_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    horizon_end_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    predicted_direction: Mapped[str] = mapped_column(String(8), nullable=False)
    predicted_conf: Mapped[float | None] = mapped_column(Float(), nullable=True)
    # Filled by the backfill once the horizon has closed; NULL until then.
    realized_move_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    # Non-authoritative debug hint (refreshed at backfill time): whether the
    # forecast became a position. NOT used for filtering — the outcomes endpoint is
    # authoritative via the live position join — so it carries no index.
    entered: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
