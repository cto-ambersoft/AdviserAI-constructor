from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TelegramNotificationDelivery(Base, TimestampMixin):
    """Idempotency + retry ledger for Telegram notifications.

    ``event_id`` is the primary key because every ``auto_trade_events`` row
    belongs to exactly one user and yields at most one notification. The
    presence of a row means the event has been processed; the dispatcher uses
    this to avoid duplicate sends across runs and to retry ``failed`` rows.
    """

    __tablename__ = "telegram_notification_deliveries"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("auto_trade_events.id"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # pending | sent | failed | skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
