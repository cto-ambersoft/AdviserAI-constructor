from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class LivePaperTrade(Base, TimestampMixin):
    __tablename__ = "live_paper_trades"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "strategy_revision",
            "entry_time",
            "exit_time",
            "side",
            "entry_price",
            name="uq_live_paper_trades_dedupe",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("live_paper_profiles.id"),
        nullable=False,
        index=True,
    )
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id"), nullable=False, index=True
    )
    strategy_revision: Mapped[int] = mapped_column(nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    entry_price: Mapped[float] = mapped_column(Float(), nullable=False)
    exit_price: Mapped[float] = mapped_column(Float(), nullable=False)
    pnl_usdt: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="closed")
    raw_payload: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
