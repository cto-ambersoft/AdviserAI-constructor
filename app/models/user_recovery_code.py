from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserRecoveryCode(Base, TimestampMixin):
    """One-time 2FA recovery code (stored hashed).

    A fallback for when the authenticator app is unavailable. Each code is consumed
    on its first successful use (``used_at`` set), and the whole set is regenerated
    on re-enrollment and cleared on disable.
    """

    __tablename__ = "user_recovery_code"
    __table_args__ = (UniqueConstraint("user_id", "code_hash", name="uq_user_recovery_code"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
