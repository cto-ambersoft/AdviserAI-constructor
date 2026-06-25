from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AutoTradeRiskConfig(Base, TimestampMixin):
    """Per-strategy Pre-Trade Risk Engine limits (W8).

    A 1:1 satellite of :class:`AutoTradeConfig` (the ``config_id`` primary key
    is also the foreign key). Kept in its own table rather than as columns on
    ``auto_trade_configs`` so the risk layer stays isolated and the hot config
    row stays lean — the migration is purely additive.

    Every limit is **nullable**: ``NULL`` means "rule off". A missing row (no
    record at all) is equivalent to every limit off, so legacy configs and
    configs created without a ``risk`` block are fail-safe by construction —
    the engine never *opens* a trade because of an absent/under-specified
    risk row.
    """

    __tablename__ = "auto_trade_risk_configs"
    __table_args__ = (
        CheckConstraint(
            "daily_loss_limit_usdt IS NULL OR daily_loss_limit_usdt >= 0",
            name="ck_at_risk_daily_loss_usdt_nonneg",
        ),
        CheckConstraint(
            "daily_loss_limit_pct IS NULL OR daily_loss_limit_pct > 0",
            name="ck_at_risk_daily_loss_pct_pos",
        ),
        # Upper bounds mirror the API schema so the DB is a real backstop even
        # for non-Pydantic writers (review I4).
        CheckConstraint(
            "daily_loss_limit_pct IS NULL OR daily_loss_limit_pct <= 100",
            name="ck_at_risk_daily_loss_pct_max",
        ),
        CheckConstraint(
            "max_open_positions IS NULL OR max_open_positions >= 1",
            name="ck_at_risk_max_open_min",
        ),
        CheckConstraint(
            "max_open_positions_per_symbol IS NULL OR max_open_positions_per_symbol >= 1",
            name="ck_at_risk_max_open_sym_min",
        ),
        CheckConstraint(
            "exposure_cap_usdt IS NULL OR exposure_cap_usdt > 0",
            name="ck_at_risk_exposure_pos",
        ),
        CheckConstraint(
            "leverage_ceiling IS NULL OR leverage_ceiling >= 1",
            name="ck_at_risk_leverage_min",
        ),
        CheckConstraint(
            "leverage_ceiling IS NULL OR leverage_ceiling <= 125",
            name="ck_at_risk_leverage_max",
        ),
        CheckConstraint(
            "conflicting_signal_policy IN ('off', 'block_opposite')",
            name="ck_at_risk_conflicting_policy",
        ),
        # --- W9 KPI-Guard auto-pause thresholds (AC#4) ---
        CheckConstraint(
            "kpi_guard_max_dd_pct IS NULL OR kpi_guard_max_dd_pct > 0",
            name="ck_at_risk_kpi_max_dd_pos",
        ),
        CheckConstraint(
            "kpi_guard_max_dd_pct IS NULL OR kpi_guard_max_dd_pct <= 100",
            name="ck_at_risk_kpi_max_dd_max",
        ),
        CheckConstraint(
            "kpi_guard_max_daily_loss_usdt IS NULL OR kpi_guard_max_daily_loss_usdt >= 0",
            name="ck_at_risk_kpi_daily_loss_usdt_nonneg",
        ),
        CheckConstraint(
            "kpi_guard_max_daily_loss_pct IS NULL OR kpi_guard_max_daily_loss_pct > 0",
            name="ck_at_risk_kpi_daily_loss_pct_pos",
        ),
        CheckConstraint(
            "kpi_guard_max_daily_loss_pct IS NULL OR kpi_guard_max_daily_loss_pct <= 100",
            name="ck_at_risk_kpi_daily_loss_pct_max",
        ),
        CheckConstraint(
            "kpi_guard_min_win_rate_pct IS NULL OR kpi_guard_min_win_rate_pct >= 0",
            name="ck_at_risk_kpi_min_wr_nonneg",
        ),
        CheckConstraint(
            "kpi_guard_min_win_rate_pct IS NULL OR kpi_guard_min_win_rate_pct <= 100",
            name="ck_at_risk_kpi_min_wr_max",
        ),
        CheckConstraint(
            "kpi_guard_min_trades IS NULL OR kpi_guard_min_trades >= 1",
            name="ck_at_risk_kpi_min_trades_min",
        ),
        # --- W9 Volatility Kill-Switch thresholds (AC#4, in-trade) ---
        CheckConstraint(
            "kill_switch_atr_spike_mult IS NULL OR kill_switch_atr_spike_mult > 1",
            name="ck_at_risk_ks_atr_spike_mult_min",
        ),
        CheckConstraint(
            "kill_switch_atr_period IS NULL OR kill_switch_atr_period >= 2",
            name="ck_at_risk_ks_atr_period_min",
        ),
        CheckConstraint(
            "kill_switch_price_move_pct IS NULL OR kill_switch_price_move_pct > 0",
            name="ck_at_risk_ks_price_move_pos",
        ),
        CheckConstraint(
            "kill_switch_cooldown_seconds IS NULL OR kill_switch_cooldown_seconds >= 0",
            name="ck_at_risk_ks_cooldown_nonneg",
        ),
        # --- B6 (W12) Strategy Anomaly Detection thresholds ---
        CheckConstraint(
            "anomaly_z_threshold IS NULL OR (anomaly_z_threshold > 0 AND anomaly_z_threshold <= 20)",
            name="ck_at_risk_anomaly_z_threshold_pos",
        ),
        CheckConstraint(
            "anomaly_window IS NULL OR (anomaly_window >= 2 AND anomaly_window <= 1000)",
            name="ck_at_risk_anomaly_window_min",
        ),
        # --- B5 (W10) Strategy Promotion KPI-Gate thresholds ---
        CheckConstraint(
            "promote_min_win_rate_pct IS NULL OR "
            "(promote_min_win_rate_pct >= 0 AND promote_min_win_rate_pct <= 100)",
            name="ck_at_risk_promote_min_wr_bounds",
        ),
        CheckConstraint(
            "promote_max_dd_pct IS NULL OR "
            "(promote_max_dd_pct > 0 AND promote_max_dd_pct <= 100)",
            name="ck_at_risk_promote_max_dd_bounds",
        ),
        CheckConstraint(
            "promote_min_trades IS NULL OR promote_min_trades >= 1",
            name="ck_at_risk_promote_min_trades_min",
        ),
        CheckConstraint(
            "promote_min_sandbox_days IS NULL OR promote_min_sandbox_days >= 0",
            name="ck_at_risk_promote_min_sandbox_days_nonneg",
        ),
    )

    config_id: Mapped[int] = mapped_column(
        ForeignKey("auto_trade_configs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    daily_loss_limit_usdt: Mapped[float | None] = mapped_column(Float(), nullable=True)
    daily_loss_limit_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    max_open_positions: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    max_open_positions_per_symbol: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    exposure_cap_usdt: Mapped[float | None] = mapped_column(Float(), nullable=True)
    leverage_ceiling: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    conflicting_signal_policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="off",
    )
    # W9 KPI-Guard auto-pause thresholds (AC#4) — opt-in, conservative, all off
    # by default (master switch off + every limit NULL). See the schema for why
    # these are distinct from the pre-trade daily_loss_limit_* above.
    kpi_guard_enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    kpi_guard_max_dd_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    kpi_guard_max_daily_loss_usdt: Mapped[float | None] = mapped_column(Float(), nullable=True)
    kpi_guard_max_daily_loss_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    kpi_guard_min_win_rate_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    kpi_guard_min_trades: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    # W9 Volatility Kill-Switch (AC#4, in-trade) — opt-in (master switch off by
    # default); the params are nullable, NULL ⇒ the detector's engine default
    # (atr_period 14, cooldown 3s). A hard auto-close fires on a confirmed spike.
    kill_switch_enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    kill_switch_atr_spike_mult: Mapped[float | None] = mapped_column(Float(), nullable=True)
    kill_switch_atr_period: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    kill_switch_price_move_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    kill_switch_cooldown_seconds: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    # B6 (W12) Strategy Anomaly Detection — opt-in (master switch off by default,
    # alert-only). Thresholds None ⇒ detector engine default (z 3.0 / window 20).
    anomaly_detection_enabled: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False
    )
    anomaly_z_threshold: Mapped[float | None] = mapped_column(Float(), nullable=True)
    anomaly_window: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    # B5 (W10) Promotion KPI-Gate thresholds. NULL ⇒ the gate's conservative
    # built-in default (a promotion gate always has criteria — see kpi_gate.py).
    promote_min_win_rate_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    promote_max_dd_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    promote_min_trades: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    promote_min_sandbox_days: Mapped[float | None] = mapped_column(Float(), nullable=True)
