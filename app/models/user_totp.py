from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserTotp(Base, TimestampMixin):
    """Per-user TOTP (2FA) enrollment.

    The shared secret is stored Fernet-encrypted at rest (never in plaintext). The
    enrollment is only *active* once ``confirmed_at`` is set — which happens when the
    user proves possession by submitting a valid code. Until then 2FA is not enabled,
    so a half-finished enrollment can never lock anyone out.

    ``failed_attempts`` / ``locked_until`` implement brute-force lockout: a 6-digit
    TOTP is otherwise online-guessable, so after a configured number of failures the
    enrollment is locked for a cooldown window (C1).
    """

    __tablename__ = "user_totp"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    secret_encrypted: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
