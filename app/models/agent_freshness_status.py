from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AgentFreshnessStatus(Base, TimestampMixin):
    """Latest data-freshness snapshot for one AI agent of one analysis profile (W8).

    Written by the 4h freshness sweep (T3.2), upserted on
    ``(profile_id, agent_key)`` so each row is the *current* status rather than
    a history. ``agent_key`` is one of the core agent codes (TW/RQ/RF/NEWS/TM/
    AN/WR) or the ``__profile__`` aggregate. A profile/agent with no underlying
    data yet stores ``last_data_at = NULL``, ``age_minutes = NULL``,
    ``is_fresh = False``.
    """

    __tablename__ = "agent_freshness_status"
    __table_args__ = (
        UniqueConstraint("profile_id", "agent_key", name="uq_agent_freshness_profile_agent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("personal_analysis_profiles.id"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    last_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    age_minutes: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    is_fresh: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
