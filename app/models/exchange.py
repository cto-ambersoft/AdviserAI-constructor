from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

EXCHANGE_MODES = ("demo", "real")
EXCHANGE_MODE_DEMO = "demo"
EXCHANGE_MODE_REAL = "real"


class ExchangeCredential(Base, TimestampMixin):
    __tablename__ = "exchange_credentials"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "exchange_name", "account_label", name="uq_exchange_user_name_label"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    exchange_name: Mapped[str] = mapped_column(String(32), nullable=False)
    account_label: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default=EXCHANGE_MODE_REAL)
    encrypted_api_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    encrypted_api_secret: Mapped[str] = mapped_column(String(1024), nullable=False)
    encrypted_passphrase: Mapped[str | None] = mapped_column(String(1024), nullable=True)
