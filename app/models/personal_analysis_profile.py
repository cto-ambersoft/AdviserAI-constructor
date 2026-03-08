from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PersonalAnalysisProfile(Base, TimestampMixin):
    __tablename__ = "personal_analysis_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    query_prompt: Mapped[str | None] = mapped_column(Text(), nullable=True)
    agents: Mapped[dict[str, bool]] = mapped_column(JSON(), nullable=False, default=dict)
    agent_weights: Mapped[dict[str, float]] = mapped_column(JSON(), nullable=False, default=dict)
    interval_minutes: Mapped[int] = mapped_column(Integer(), nullable=False, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True, index=True)
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
