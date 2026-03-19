from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AutoTradeEvent(Base, TimestampMixin):
    __tablename__ = "auto_trade_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_trade_configs.id"),
        nullable=True,
        index=True,
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_analysis_profiles.id"),
        nullable=True,
        index=True,
    )
    history_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_analysis_history.id"),
        nullable=True,
        index=True,
    )
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_trade_positions.id"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
