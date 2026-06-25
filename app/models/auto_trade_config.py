from datetime import datetime

from sqlalchemy import (
    JSON,
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
        # Lower bound is 1 (not 0) to make the percent-vs-fraction unit
        # mismatch a hard error at write time. Without this, a value like
        # 0.65 (meaning "65 %") would silently disable the gate because
        # every parsed signal lands in [0, 100] and therefore exceeds 0.65.
        # The matching alembic migration (20260511_0014) backfills any
        # pre-existing rows that were stored as fractions.
        CheckConstraint(
            "min_confidence_pct >= 1 AND min_confidence_pct <= 100",
            name="ck_auto_trade_cfg_min_confidence_bounds",
        ),
        CheckConstraint(
            "fast_close_confidence_pct >= 1 AND fast_close_confidence_pct <= 100",
            name="ck_auto_trade_cfg_fast_close_confidence_bounds",
        ),
        CheckConstraint(
            "confirm_reports_required >= 1",
            name="ck_auto_trade_cfg_confirm_reports_required_min",
        ),
        CheckConstraint("risk_mode LIKE '1:%'", name="ck_auto_trade_cfg_risk_mode"),
        CheckConstraint("sl_pct > 0", name="ck_auto_trade_cfg_sl_pct_positive"),
        CheckConstraint("tp_pct > 0", name="ck_auto_trade_cfg_tp_pct_positive"),
        # B5 (W10) Strategy Promotion lifecycle stage — mirrors the FSM stage set
        # in app/services/auto_trade/promotion/state_machine.py.
        CheckConstraint(
            "lifecycle_stage IN "
            "('research', 'sandbox', 'validation', 'live', 'rejected', 'archived')",
            name="ck_auto_trade_cfg_lifecycle_stage",
        ),
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
    strategy_profile_json: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    ai_overlay_config_json: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    # W7: optional human-readable label shown in the multi-strategy UI selector.
    # NULL → frontend falls back to the profile symbol.
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # T16 (W12e/AC#1): catalogue forecast this live strategy is attached to —
    # provenance link to the core ``ai_forecast_catalogue.forecastId``. NULL → none.
    attached_forecast_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # B5 (W10) / T8 (W10e): promotion lifecycle stage. Defaults to 'sandbox'
    # (server + ORM) — fail-safe: a config created by any path (not just
    # upsert_config) must never default into real-money 'live'. New strategies earn
    # 'live' through the KPI gate. Existing rows are left untouched by migration 0036.
    lifecycle_stage: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="sandbox", default="sandbox"
    )
    # B5 (W10): when the strategy entered sandbox — drives the KPI-Gate minimum
    # sandbox-period check. NULL ⇒ never sandboxed (service falls back to created_at).
    sandbox_entered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A3 (audit §2.5.9): persisted Volatility Kill-Switch risk-off latch. The
    # kill-switch already pauses the strategy (is_running=False); these columns
    # record *why* and *when* so the latch survives a process restart and an
    # operator can see it. Cleared on a manual resume (set_running True).
    risk_off_latched: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    risk_off_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def strategy_profile(self) -> dict[str, object] | None:
        return self.strategy_profile_json

    @property
    def ai_overlay(self) -> dict[str, object] | None:
        return self.ai_overlay_config_json
