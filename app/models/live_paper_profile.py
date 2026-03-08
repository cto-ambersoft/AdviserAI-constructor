from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class LivePaperProfile(Base, TimestampMixin):
    __tablename__ = "live_paper_profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_live_paper_profiles_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), nullable=False, index=True)
    strategy_revision: Mapped[int] = mapped_column(nullable=False, default=1)
    is_running: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    total_balance_usdt: Mapped[float] = mapped_column(Float(), nullable=False, default=1000.0)
    per_trade_usdt: Mapped[float] = mapped_column(Float(), nullable=False, default=100.0)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
