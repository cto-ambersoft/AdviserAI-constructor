from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AutoTradeSignalState(Base, TimestampMixin):
    __tablename__ = "auto_trade_signal_state"
    __table_args__ = (UniqueConstraint("config_id", name="uq_auto_trade_signal_state_config_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("auto_trade_configs.id"),
        nullable=False,
        index=True,
    )
    last_processed_history_id: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    last_trend: Mapped[str | None] = mapped_column(String(16), nullable=True)
    opposite_streak: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    last_signal_confidence_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
