from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserEmailConfirmation(Base, TimestampMixin):
    """One-time email confirmation code for a critical action (T20 / W11c).

    A second factor alongside step-up: a code is emailed (via Resend) and verified
    to authorize an action. Single-use (``consumed_at``) and TTL-bounded
    (``expires_at``); the code is stored hashed at rest, never in plaintext.
    """

    __tablename__ = "user_email_confirmations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # The critical action this code authorizes (e.g. "change_exchange_key").
    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
