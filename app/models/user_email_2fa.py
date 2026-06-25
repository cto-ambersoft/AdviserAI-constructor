from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserEmail2FA(Base, TimestampMixin):
    """Per-user email-2FA enrollment, mirroring :class:`UserTotp`.

    Email is a co-equal, opt-in second factor: the enrollment is only *active* once
    ``confirmed_at`` is set, which happens when the user proves control of the account
    email by submitting a code sent to it (verify-on-enroll). Until then email-2FA is
    not considered enabled, so a half-finished enrollment can never lock anyone out.

    There is no secret to store (codes are delivered/verified via the ``email_confirm``
    service, hashed and single-use). ``failed_attempts`` / ``locked_until`` implement
    the same per-factor brute-force lockout as TOTP: an emailed code is otherwise
    online-guessable within its TTL, so after a configured number of failures the
    factor is locked for a cooldown window.
    """

    __tablename__ = "user_email_2fa"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
