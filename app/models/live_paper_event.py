from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class LivePaperEvent(Base, TimestampMixin):
    __tablename__ = "live_paper_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("live_paper_profiles.id"),
        nullable=False,
        index=True,
    )
    strategy_revision: Mapped[int] = mapped_column(nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
