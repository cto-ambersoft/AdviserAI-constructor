from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ExchangeOrderMetadata(Base, TimestampMixin):
    __tablename__ = "exchange_order_metadata"
    __table_args__ = (
        CheckConstraint(
            "source IN ('auto_trade_open', 'auto_trade_close', 'manual', 'unknown')",
            name="ck_exchange_order_metadata_source",
        ),
        Index(
            "ix_exchange_order_metadata_account_exchange_order",
            "account_id",
            "exchange_order_id",
        ),
        Index(
            "ix_exchange_order_metadata_account_client_order",
            "account_id",
            "client_order_id",
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
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_trade_configs.id"),
        nullable=True,
        index=True,
    )
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_trade_positions.id"),
        nullable=True,
        index=True,
    )
    history_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_analysis_history.id"),
        nullable=True,
        index=True,
    )
