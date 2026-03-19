from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExchangeTradeLedger(Base):
    __tablename__ = "exchange_trade_ledger"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_exchange_trade_ledger_price_non_negative"),
        CheckConstraint("amount >= 0", name="ck_exchange_trade_ledger_amount_non_negative"),
        CheckConstraint("fee_cost >= 0", name="ck_exchange_trade_ledger_fee_non_negative"),
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_exchange_trade_ledger_market_type",
        ),
        CheckConstraint(
            "origin IN ('platform', 'external', 'unknown')",
            name="ck_exchange_trade_ledger_origin",
        ),
        CheckConstraint(
            "origin_confidence IN ('strong', 'weak', 'none')",
            name="ck_exchange_trade_ledger_origin_confidence",
        ),
        UniqueConstraint(
            "account_id",
            "exchange_name",
            "market_type",
            "symbol",
            "exchange_trade_id",
            name="uq_exchange_trade_ledger_trade_identity",
        ),
        Index(
            "ix_exchange_trade_ledger_account_symbol_traded_at",
            "account_id",
            "symbol",
            "traded_at",
        ),
        Index(
            "ix_exchange_trade_ledger_account_exchange_order_id",
            "account_id",
            "exchange_order_id",
        ),
        Index(
            "ix_exchange_trade_ledger_account_client_order_id",
            "account_id",
            "client_order_id",
        ),
        Index(
            "ix_exchange_trade_ledger_position_traded_at",
            "auto_trade_position_id",
            "traded_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_credentials.id"),
        nullable=False,
        index=True,
    )
    exchange_name: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="futures")
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    exchange_trade_id: Mapped[str] = mapped_column(String(128), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[float] = mapped_column(Float(), nullable=False)
    amount: Mapped[float] = mapped_column(Float(), nullable=False)
    cost: Mapped[float | None] = mapped_column(Float(), nullable=True)
    fee_cost: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    fee_currency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    origin_confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    auto_trade_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_trade_configs.id"),
        nullable=True,
        index=True,
    )
    auto_trade_position_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_trade_positions.id"),
        nullable=True,
        index=True,
    )
    open_history_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_analysis_history.id"),
        nullable=True,
        index=True,
    )
    close_history_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_analysis_history.id"),
        nullable=True,
        index=True,
    )
    raw_trade: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
