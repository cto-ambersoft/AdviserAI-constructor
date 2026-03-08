from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PersonalAnalysisJob(Base, TimestampMixin):
    __tablename__ = "personal_analysis_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("personal_analysis_profiles.id"),
        nullable=False,
        index=True,
    )
    core_job_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="pending")
    attempt: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer(), nullable=False, default=3)
    error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
    next_poll_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    core_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
