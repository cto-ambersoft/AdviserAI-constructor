from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AutoTradePosition(Base, TimestampMixin):
    __tablename__ = "auto_trade_positions"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_auto_trade_pos_quantity_positive"),
        CheckConstraint("entry_price > 0", name="ck_auto_trade_pos_entry_price_positive"),
        CheckConstraint(
            "position_size_usdt > 0",
            name="ck_auto_trade_pos_position_size_positive",
        ),
        CheckConstraint("leverage >= 1", name="ck_auto_trade_pos_leverage_min"),
        CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_auto_trade_pos_side"),
        CheckConstraint(
            "status IN ('open', 'closed', 'error')",
            name="ck_auto_trade_pos_status",
        ),
        Index(
            "uq_auto_trade_positions_user_account_open",
            "user_id",
            "account_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("auto_trade_configs.id"),
        nullable=False,
        index=True,
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("personal_analysis_profiles.id"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_credentials.id"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="open")
    entry_price: Mapped[float] = mapped_column(Float(), nullable=False)
    quantity: Mapped[float] = mapped_column(Float(), nullable=False)
    position_size_usdt: Mapped[float] = mapped_column(Float(), nullable=False)
    leverage: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    tp_price: Mapped[float] = mapped_column(Float(), nullable=False)
    sl_price: Mapped[float] = mapped_column(Float(), nullable=False)
    entry_confidence_pct: Mapped[float] = mapped_column(Float(), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float(), nullable=True)
    open_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    close_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
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
    raw_open_order: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
    raw_close_order: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False, default=dict)
