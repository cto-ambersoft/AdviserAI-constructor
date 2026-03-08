from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PersonalAnalysisHistory(Base, TimestampMixin):
    __tablename__ = "personal_analysis_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("personal_analysis_profiles.id"),
        nullable=False,
        index=True,
    )
    trade_job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("personal_analysis_jobs.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    analysis_data: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
    core_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
