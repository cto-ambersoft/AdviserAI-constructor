from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TelegramNotificationSettings(Base, TimestampMixin):
    """Per-user Telegram notification preferences and link state.

    A user is considered *linked* once ``chat_id`` is populated (set by the
    webhook after a successful ``/start <link_code>``). ``enabled`` is the
    master switch; the ``notify_on_*`` flags gate individual event families.
    """

    __tablename__ = "telegram_notification_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    # Telegram chat ids can exceed 32 bits and are negative for groups.
    chat_id: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    notify_on_open: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    notify_on_close: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    notify_on_risk: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    # One-time deep-link code; cleared once consumed by the webhook.
    link_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    link_code_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
