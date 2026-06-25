from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
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
        # W7: prevent two credentials of the same user from pointing at the
        # physical sub-account (same api_key under two different labels).
        # Partial so legacy rows that lack a hash do not violate the index;
        # the credentials service backfills and writes the hash going forward.
        Index(
            "uq_exchange_credentials_user_api_key_hash",
            "user_id",
            "api_key_hash",
            unique=True,
            postgresql_where=text("api_key_hash IS NOT NULL"),
            sqlite_where=text("api_key_hash IS NOT NULL"),
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
    # W7: sha256(decrypted_api_key) used to detect the same physical
    # exchange sub-account being registered twice by one user. The partial
    # unique index in __table_args__ covers all queries that filter by hash.
    api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
