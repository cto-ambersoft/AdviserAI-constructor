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


class ExchangeIncomeLedger(Base):
    """Exchange account income rows (Binance ``/fapi/v1/income``).

    Currently only ``FUNDING_FEE`` is synced (commission lives on the trade
    ledger's ``fee_cost``; realized lives on ``realized_pnl``). ``tranId`` is
    unique per (user, income type) on Binance, so the idempotency key is
    (account_id, exchange_name, income_type, tran_id). ``income`` is signed:
    positive is an inflow (funding received), negative an outflow (funding paid).
    """

    __tablename__ = "exchange_income_ledger"
    __table_args__ = (
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_exchange_income_ledger_market_type",
        ),
        UniqueConstraint(
            "account_id",
            "exchange_name",
            "income_type",
            "tran_id",
            name="uq_exchange_income_ledger_identity",
        ),
        Index(
            "ix_exchange_income_ledger_account_symbol_income_at",
            "account_id",
            "symbol",
            "income_at",
        ),
        Index(
            "ix_exchange_income_ledger_account_type_income_at",
            "account_id",
            "income_type",
            "income_at",
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
    income_type: Mapped[str] = mapped_column(String(32), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    income: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tran_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    info: Mapped[str | None] = mapped_column(String(64), nullable=True)
    income_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
