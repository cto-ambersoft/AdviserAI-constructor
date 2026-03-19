from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExchangeTradeSyncState(Base):
    __tablename__ = "exchange_trade_sync_state"
    __table_args__ = (
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_exchange_trade_sync_state_market_type",
        ),
        CheckConstraint("error_count >= 0", name="ck_exchange_trade_sync_state_error_count_min"),
        UniqueConstraint(
            "account_id",
            "symbol",
            "market_type",
            name="uq_exchange_trade_sync_state_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_credentials.id"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="futures")
    last_trade_ts_ms: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    last_trade_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_backfill_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
