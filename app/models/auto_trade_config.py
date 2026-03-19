from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AutoTradeConfig(Base, TimestampMixin):
    __tablename__ = "auto_trade_configs"
    __table_args__ = (
        UniqueConstraint("user_id", "account_id", name="uq_auto_trade_configs_user_account_id"),
        CheckConstraint("position_size_usdt > 0", name="ck_auto_trade_cfg_position_size_positive"),
        CheckConstraint("leverage >= 1", name="ck_auto_trade_cfg_leverage_min"),
        CheckConstraint(
            "min_confidence_pct >= 0 AND min_confidence_pct <= 100",
            name="ck_auto_trade_cfg_min_confidence_bounds",
        ),
        CheckConstraint(
            "fast_close_confidence_pct >= 0 AND fast_close_confidence_pct <= 100",
            name="ck_auto_trade_cfg_fast_close_confidence_bounds",
        ),
        CheckConstraint(
            "confirm_reports_required >= 1",
            name="ck_auto_trade_cfg_confirm_reports_required_min",
        ),
        CheckConstraint("risk_mode LIKE '1:%'", name="ck_auto_trade_cfg_risk_mode"),
        CheckConstraint("sl_pct > 0", name="ck_auto_trade_cfg_sl_pct_positive"),
        CheckConstraint("tp_pct > 0", name="ck_auto_trade_cfg_tp_pct_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
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
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    is_running: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    position_size_usdt: Mapped[float] = mapped_column(Float(), nullable=False, default=100.0)
    leverage: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    min_confidence_pct: Mapped[float] = mapped_column(Float(), nullable=False, default=62.0)
    fast_close_confidence_pct: Mapped[float] = mapped_column(Float(), nullable=False, default=80.0)
    confirm_reports_required: Mapped[int] = mapped_column(Integer(), nullable=False, default=2)
    risk_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="1:2")
    sl_pct: Mapped[float] = mapped_column(Float(), nullable=False, default=1.0)
    tp_pct: Mapped[float] = mapped_column(Float(), nullable=False, default=2.0)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
