from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AutoTradeSignalQueue(Base, TimestampMixin):
    __tablename__ = "auto_trade_signal_queue"
    __table_args__ = (
        UniqueConstraint(
            "history_id", "config_id", name="uq_auto_trade_signal_queue_history_config_id"
        ),
        CheckConstraint("attempt >= 0", name="ck_auto_trade_queue_attempt_min"),
        CheckConstraint("max_attempts >= 1", name="ck_auto_trade_queue_max_attempts_min"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'dead')",
            name="ck_auto_trade_queue_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("auto_trade_configs.id"),
        nullable=False,
        index=True,
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("personal_analysis_profiles.id"),
        nullable=False,
        index=True,
    )
    history_id: Mapped[int] = mapped_column(
        ForeignKey("personal_analysis_history.id"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempt: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer(), nullable=False, default=5)
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
