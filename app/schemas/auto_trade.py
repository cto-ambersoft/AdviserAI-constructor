import math
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.strategy_profile import StrategyProfileConfig

RiskMode = str
PositionSide = Literal["LONG", "SHORT"]
PositionStatus = Literal["open", "closed", "error"]
PnlSource = Literal["exchange", "derived", "closed", "unavailable"]

_RISK_MODE_PATTERN = re.compile(r"^1:(\d+(?:[.,]\d+)?)$")


def _parse_risk_mode(risk_mode: RiskMode) -> tuple[str, float]:
    raw = str(risk_mode or "").strip()
    match = _RISK_MODE_PATTERN.match(raw)
    if match is None:
        raise ValueError("risk_mode must look like 1:2 or 1:2.5.")
    ratio_raw = match.group(1).replace(",", ".")
    try:
        ratio = float(ratio_raw)
    except ValueError as exc:
        raise ValueError("risk_mode must contain a valid ratio.") from exc
    if ratio <= 0 or not math.isfinite(ratio):
        raise ValueError("risk_mode ratio must be a positive finite number.")
    normalized = f"1:{ratio:g}"
    return normalized, ratio


class AutoTradeRiskConfig(BaseModel):
    """Pre-Trade Risk Engine limits for a strategy (W8).

    Every limit is optional; ``None`` means "rule off". The model is used both
    as nested input on the config upsert and (with ``from_attributes``) as the
    serialized form of the :class:`app.models.auto_trade_risk_config.\
AutoTradeRiskConfig` row on read. Bounds here mirror the table-level
    CheckConstraints so a bad value is rejected at the API edge with a clear
    422 rather than surfacing as a database error.
    """

    model_config = ConfigDict(from_attributes=True)

    enabled: bool = True
    daily_loss_limit_usdt: float | None = Field(default=None, ge=0)
    daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=100)
    max_open_positions: int | None = Field(default=None, ge=1)
    max_open_positions_per_symbol: int | None = Field(default=None, ge=1)
    exposure_cap_usdt: float | None = Field(default=None, gt=0)
    leverage_ceiling: int | None = Field(default=None, ge=1, le=125)
    # 'off' (the default) means conflict blocking is opt-in — setting another
    # risk limit must not silently start blocking opposite signals (review I5).
    conflicting_signal_policy: Literal["off", "block_opposite"] = "off"

    # --- W9 KPI-Guard auto-pause thresholds (AC#4) ---
    # DISTINCT from the pre-trade daily_loss_limit_* above: those *block the next
    # entry*; these *pause the whole strategy* when live KPIs breach, and should
    # be set more conservatively (a one-trade overshoot is not a halt). Opt-in:
    # kpi_guard_enabled=False ⇒ guard off; any threshold None ⇒ that rule off.
    # Bounds mirror the table CheckConstraints so a bad value is a 422 at the edge.
    kpi_guard_enabled: bool = False
    kpi_guard_max_dd_pct: float | None = Field(default=None, gt=0, le=100)
    kpi_guard_max_daily_loss_usdt: float | None = Field(default=None, ge=0)
    kpi_guard_max_daily_loss_pct: float | None = Field(default=None, gt=0, le=100)
    kpi_guard_min_win_rate_pct: float | None = Field(default=None, ge=0, le=100)
    kpi_guard_min_trades: int | None = Field(default=None, ge=1)

    # --- W9 Volatility Kill-Switch (AC#4, in-trade hard auto-close) ---
    # Opt-in: kill_switch_enabled=False ⇒ off. Thresholds None ⇒ that branch off;
    # atr_period / cooldown_seconds None ⇒ engine default (14 bars / 3s). Bounds
    # mirror the table CheckConstraints.
    kill_switch_enabled: bool = False
    kill_switch_atr_spike_mult: float | None = Field(default=None, gt=1)
    kill_switch_atr_period: int | None = Field(default=None, ge=2)
    kill_switch_price_move_pct: float | None = Field(default=None, gt=0)
    kill_switch_cooldown_seconds: int | None = Field(default=None, ge=0)

    # --- B6 (W12) Strategy Anomaly Detection ---
    # Opt-in: anomaly_detection_enabled=False ⇒ off (alert-only when on). Params
    # None ⇒ detector engine default (z-threshold 3.0 / window 20). Bounds mirror
    # the table CheckConstraints.
    anomaly_detection_enabled: bool = False
    anomaly_z_threshold: float | None = Field(default=None, gt=0, le=20)
    # Upper bound keeps the rolling window bounded (a window larger than the
    # series silently disables detection); mirrors the table CheckConstraint.
    anomaly_window: int | None = Field(default=None, ge=2, le=1000)

    # --- B5 (W10) Strategy Promotion KPI-Gate thresholds ---
    # NULL ⇒ the gate's conservative built-in default (a promotion gate always
    # has criteria, unlike kpi_guard where NULL ⇒ rule off). Bounds mirror the
    # table CheckConstraints.
    promote_min_win_rate_pct: float | None = Field(default=None, ge=0, le=100)
    promote_max_dd_pct: float | None = Field(default=None, gt=0, le=100)
    promote_min_trades: int | None = Field(default=None, ge=1)
    promote_min_sandbox_days: float | None = Field(default=None, ge=0)


class AutoTradeConfigUpsertRequest(BaseModel):
    enabled: bool = False
    profile_id: int = Field(gt=0)
    account_id: int = Field(gt=0)
    position_size_usdt: float = Field(gt=0)
    leverage: int = Field(default=1, ge=1, le=125)
    # Both confidence thresholds are stored and compared as percentages
    # in [1, 100]. ``ge=1`` (not ge=0) closes the unit-mismatch foot-gun
    # where a UI submitted 0.65 meaning "65 %" and the gate at
    # ``service.py:_process_without_open_position`` silently let every
    # signal through (because every real signal lies in [0, 100] and
    # 56 >= 0.65 is true). The model-level validator below additionally
    # rejects any value strictly between 0 and 1 with a self-explanatory
    # error message.
    min_confidence_pct: float = Field(default=62.0, ge=1, le=100)
    fast_close_confidence_pct: float = Field(default=80.0, ge=1, le=100)
    confirm_reports_required: int = Field(default=2, ge=1, le=5)
    risk_mode: RiskMode = "1:2"
    sl_pct: float = Field(gt=0)
    tp_pct: float = Field(gt=0)
    strategy_profile: StrategyProfileConfig | None = None
    # W7: optional UI label for the multi-strategy switcher. ``None`` (or
    # absent) means "fall back to the profile symbol" on the frontend.
    strategy_name: str | None = Field(default=None, max_length=64)
    # T16 (W12e/AC#1): catalogue forecast to attach this live strategy to
    # (provenance link to core's forecastId). Absent leaves it untouched on update.
    attached_forecast_id: str | None = Field(default=None, max_length=64)
    # W8: optional Pre-Trade Risk Engine limits. Absent/``None`` leaves any
    # existing risk row untouched on update and creates none on insert.
    risk: AutoTradeRiskConfig | None = None

    @model_validator(mode="after")
    def validate_risk_mode_ratio(self) -> "AutoTradeConfigUpsertRequest":
        # Belt-and-suspenders: ``Field(ge=1)`` already rejects values in
        # (0, 1), but pydantic surfaces those errors as a terse range
        # message. The explicit check here produces a "Did you mean N?"
        # hint that points operators at the typical 0.65/65 confusion.
        for field_name in ("min_confidence_pct", "fast_close_confidence_pct"):
            value = float(getattr(self, field_name))
            if 0 < value < 1.0:
                suggested = value * 100.0
                raise ValueError(
                    f"{field_name} must be a percentage in [1, 100] (got {value}). "
                    f"Did you mean {suggested:g}? Confidence thresholds are stored "
                    f"as percents, not fractions."
                )
        if self.fast_close_confidence_pct < self.min_confidence_pct:
            raise ValueError(
                "fast_close_confidence_pct must be greater than or equal to min_confidence_pct."
            )
        normalized, expected = _parse_risk_mode(self.risk_mode)
        self.risk_mode = normalized
        actual = self.tp_pct / self.sl_pct
        if abs(actual - expected) > 0.01:
            raise ValueError(
                f"tp_pct/sl_pct must match risk_mode {self.risk_mode} (expected {expected:.2f})."
            )
        return self


class AutoTradeConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    profile_id: int
    account_id: int
    enabled: bool
    is_running: bool
    position_size_usdt: float
    leverage: int
    min_confidence_pct: float
    fast_close_confidence_pct: float
    confirm_reports_required: int
    risk_mode: RiskMode
    sl_pct: float
    tp_pct: float
    strategy_profile: StrategyProfileConfig | None = None
    strategy_name: str | None = None
    attached_forecast_id: str | None = None
    risk: AutoTradeRiskConfig | None = None
    # B5 (W10): promotion lifecycle stage. Literal mirrors the FSM stage set
    # (LifecycleStage) and the DB CHECK — same pattern as conflicting_signal_policy.
    lifecycle_stage: Literal[
        "research", "sandbox", "validation", "live", "rejected", "archived"
    ] = "live"
    last_started_at: datetime | None
    last_stopped_at: datetime | None
    # A3: persisted Volatility Kill-Switch risk-off latch (survives restarts).
    risk_off_latched: bool = False
    risk_off_reason: str | None = None
    risk_off_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RiskConfigBulkApplyResponse(BaseModel):
    """Result of applying one risk config to all of a user's strategies (A2)."""

    updated_count: int


class PromotionGateCriterionRead(BaseModel):
    """One promotion-gate criterion with its actual value vs threshold (B5)."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    actual: float
    threshold: float
    passed: bool


class PromotionStatusRead(BaseModel):
    """Current lifecycle stage + KPI-Gate readiness for a strategy (B5 — W10)."""

    config_id: int
    lifecycle_stage: str
    sandbox_days: float
    can_promote: bool
    criteria: list[PromotionGateCriterionRead]


class AutoTradePositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    config_id: int
    profile_id: int
    account_id: int
    symbol: str
    side: PositionSide
    status: PositionStatus
    entry_price: float
    quantity: float
    position_size_usdt: float
    leverage: int
    tp_price: float
    sl_price: float
    entry_confidence_pct: float
    opened_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    close_price: float | None
    open_order_id: str | None
    close_order_id: str | None
    open_history_id: int | None
    close_history_id: int | None
    # W2 traceability: ID of the AI decision document in core's
    # ai_decision_events collection that drove this entry (when overlay
    # was active at open time). Nullable for legacy/non-overlay positions.
    decision_event_id: str | None = None
    raw_open_order: dict[str, Any]
    raw_close_order: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AutoTradePositionPnlRead(BaseModel):
    position_id: int
    symbol: str
    chart_symbol: str
    side: PositionSide
    status: PositionStatus
    entry_price: float
    mark_price: float | None = None
    close_price: float | None = None
    quantity: float
    entry_notional_usdt: float
    initial_margin_usdt: float
    realized_pnl_usdt: float | None = None
    unrealized_pnl_usdt: float | None = None
    total_pnl_usdt: float | None = None
    # Explicit PnL decomposition (populated when the position has synced ledger
    # fills): net = gross_realized − commission + funding. Null on the legacy
    # fallback path where only an aggregate realized is available.
    gross_realized_usdt: float | None = None
    commission_usdt: float | None = None
    funding_usdt: float | None = None
    net_pnl_usdt: float | None = None
    pnl_pct: float | None = None
    roe_pct: float | None = None
    source: PnlSource
    error: str | None = None
    calculated_at: datetime


class AutoTradePositionWithPnlRead(BaseModel):
    position: AutoTradePositionRead
    pnl: AutoTradePositionPnlRead
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    trade_pnl_usdt: float | None = None


class AutoTradePositionsSummaryRead(BaseModel):
    total_positions: int = Field(ge=0)
    open_positions: int = Field(ge=0)
    closed_positions: int = Field(ge=0)
    total_realized_pnl_usdt: float
    total_unrealized_pnl_usdt: float
    total_pnl_usdt: float
    total_trade_pnl_usdt: float


class AutoTradeEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    config_id: int | None
    profile_id: int | None
    history_id: int | None
    position_id: int | None
    event_type: str
    level: str
    message: str | None
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class StrategyHealthRead(BaseModel):
    """On-read composite health for one strategy (W8 — T2.1)."""

    model_config = ConfigDict(from_attributes=True)

    config_id: int
    window_days: int
    sample_size: int
    win_rate_pct: float
    max_dd_pct: float = Field(
        description=(
            "Max drawdown as a % of the per-trade notional base (position_size_usdt), "
            "NOT account equity — a W9 proxy pending real-balance calibration. UI: label "
            "accordingly; do not present as account drawdown."
        )
    )
    total_pnl_usdt: float
    roi_pct: float = Field(
        description=(
            "Realized PnL as a % of the per-trade notional base (position_size_usdt), "
            "NOT account equity — a W9 proxy (can read high). Calibrate the base to the "
            "sub-account balance before relying on it; UI must label the denominator."
        )
    )
    sharpe_proxy: float
    stability_score: float
    health_score: float
    health_class: str
    computed_at: datetime


class AutoTradePlayStopResponse(BaseModel):
    config: AutoTradeConfigRead


class AutoTradeStateResponse(BaseModel):
    config: AutoTradeConfigRead | None = None


class AutoTradeEventsResponse(BaseModel):
    events: list[AutoTradeEventRead] = Field(default_factory=list)


class PositionTraceRead(BaseModel):
    """Post-Trade execution trace for one position (W9 — T3.1).

    The signal→close timeline: position metadata + linkage pointers (the AI
    ``decision_event_id`` points at core's ``ai_decision_events`` and is surfaced,
    not dereferenced) + the chronological ``AutoTradeEvent`` list.
    """

    position_id: int
    symbol: str
    side: str
    status: str
    entry_price: float
    close_price: float | None
    close_reason: str | None
    state: str
    decision_event_id: str | None
    open_history_id: int | None
    close_history_id: int | None
    open_order_id: str | None
    close_order_id: str | None
    opened_at: datetime
    closed_at: datetime | None
    events: list[AutoTradeEventRead] = Field(default_factory=list)


class AutoTradePositionsResponse(BaseModel):
    positions: list[AutoTradePositionWithPnlRead] = Field(default_factory=list)
    summary: AutoTradePositionsSummaryRead


class AutoTradeConfigsResponse(BaseModel):
    configs: list[AutoTradeConfigRead] = Field(default_factory=list)
    active_account_id: int | None = None
    active_config: AutoTradeConfigRead | None = None


class AutoTradeLedgerTradeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    exchange_name: str
    market_type: str
    symbol: str
    exchange_trade_id: str
    exchange_order_id: str | None
    client_order_id: str | None
    side: str
    price: float
    amount: float
    cost: float | None
    fee_cost: float
    fee_currency: str | None
    traded_at: datetime
    ingested_at: datetime
    origin: str
    origin_confidence: str
    auto_trade_config_id: int | None
    auto_trade_position_id: int | None
    open_history_id: int | None
    close_history_id: int | None
    raw_trade: dict[str, Any]


class AutoTradeLedgerTradesSummaryRead(BaseModel):
    total: int = Field(ge=0)
    platform: int = Field(ge=0)
    external: int = Field(ge=0)
    total_fee_usdt: float


class AutoTradeLedgerTradesResponse(BaseModel):
    trades: list[AutoTradeLedgerTradeRead] = Field(default_factory=list)
    summary: AutoTradeLedgerTradesSummaryRead


# ─────────── Close-open-positions (manual flatten) ──────────────────────


class AutoTradeCloseOpenPositionsRequest(BaseModel):
    """Request body for the manual ``/auto-trade/close-positions`` flatten flow.

    The flow is intentionally two-step:

      1. Client sends ``confirm: false`` (or omits it). The server returns
         HTTP 412 with a preview of what would be closed — list of positions,
         their sides/quantities, and how many conditional orders would be
         cancelled. Nothing changes on the exchange or in the DB.
      2. Client reviews the preview, then re-sends with ``confirm: true``.
         The server proceeds to cancel TP/SL conditional orders and market-
         close every open ``AutoTradePosition`` for the resolved scope.

    Auto-trade ``is_running`` is **not** flipped automatically. Stop and close
    are independent operations — the user explicitly asked for them to be
    decoupled. Pending signals in the queue are also untouched.
    """

    account_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional account scope. Required only if the user owns more than "
            "one auto-trade config. Otherwise the unique config is resolved "
            "from ``user_id`` alone."
        ),
    )
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true to actually close the positions. Without it the "
            "server returns 412 + a preview and changes nothing."
        ),
    )
    reason: str | None = Field(
        default=None,
        max_length=200,
        description="Optional free-text reason recorded in the audit event.",
    )


class AutoTradeClosePreviewItem(BaseModel):
    """Single position summary shown in the 412 preview response."""

    position_id: int
    symbol: str
    side: PositionSide
    current_quantity: float
    entry_price: float
    current_sl_price: float | None
    open_conditional_orders_count: int


class AutoTradeClosePreview(BaseModel):
    """Body of the 412 response when ``confirm`` is false / missing."""

    detail: str = Field(
        default="Confirmation required: re-send with confirm=true to execute.",
    )
    positions: list[AutoTradeClosePreviewItem] = Field(default_factory=list)
    total_count: int = Field(ge=0)
    requires_confirm: bool = True


class AutoTradeClosedPositionInfo(BaseModel):
    """Per-position outcome row in the success response."""

    position_id: int
    symbol: str
    side: PositionSide
    executed_qty: float
    avg_price: float | None
    cancelled_conditional_orders: list[str] = Field(default_factory=list)


class AutoTradeFailedClosePositionInfo(BaseModel):
    position_id: int
    symbol: str
    error: str


class AutoTradeCloseOpenPositionsResponse(BaseModel):
    closed: list[AutoTradeClosedPositionInfo] = Field(default_factory=list)
    failed: list[AutoTradeFailedClosePositionInfo] = Field(default_factory=list)
    skipped_already_closed: list[int] = Field(default_factory=list)


# ─────────── W7 multi-strategy partitioning ──────────────────────────────


class StrategyPortfolioEntryRead(BaseModel):
    """One strategy slot in the aggregated portfolio view.

    Each entry corresponds to a single :class:`AutoTradeConfig` row, which by
    W7 design owns its own exchange sub-account (credential). Balance values
    are pulled live from the exchange; ``balance_error`` is populated when
    that sub-account's adapter call failed so the dashboard can show a
    degraded state instead of failing the whole portfolio fetch.
    """

    model_config = ConfigDict(from_attributes=True)

    config_id: int
    account_id: int
    account_label: str
    exchange_name: str
    mode: str
    # B5 (W10): promotion lifecycle stage (research/sandbox/validation/live/…).
    lifecycle_stage: str
    strategy_name: str | None
    profile_id: int
    profile_symbol: str | None
    is_running: bool
    enabled: bool
    open_positions_count: int
    margin_used_usdt: float
    realized_pnl_usdt: float
    unrealized_pnl_usdt: float
    balance_total_usdt: float | None
    balance_free_usdt: float | None
    last_started_at: datetime | None
    last_stopped_at: datetime | None
    balance_error: str | None
    # W9 T3.2 — live KPIs from the latest health snapshot (AC#7 dashboard).
    # None until a snapshot exists for the strategy.
    win_rate_pct: float | None
    max_dd_pct: float | None = Field(
        description=(
            "Max drawdown as a % of the per-trade notional base, NOT account equity "
            "(W9 proxy). UI must label the denominator; calibration pending."
        ),
    )
    sharpe_proxy: float | None
    roi_pct: float | None = Field(
        description=(
            "Realized PnL as a % of the per-trade notional base, NOT account equity "
            "(W9 proxy — can read high). UI must label the denominator; calibration pending."
        ),
    )
    health_class: str | None
    sample_size: int | None
    kpi_as_of: datetime | None = Field(
        default=None,
        description=(
            "When the surfaced KPIs were computed: the snapshot's computed_at when "
            "read from the cron-written snapshot, or the live recompute time when the "
            "snapshot was missing/stale for a running strategy. Null when no KPIs."
        ),
    )


class PortfolioSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    strategies: list[StrategyPortfolioEntryRead] = Field(default_factory=list)
    total_realized_pnl_usdt: float
    total_unrealized_pnl_usdt: float
    total_open_positions: int
    total_running_strategies: int
    # Worst per-strategy max-drawdown across the portfolio (from snapshots).
    portfolio_max_dd_pct: float = 0.0


class BulkLifecycleResultItem(BaseModel):
    """Per-config outcome row in ``BulkLifecycleResponse``."""

    config_id: int
    account_id: int
    strategy_name: str | None = None
    status: Literal["ok", "skipped", "failed"]
    reason: str | None = None
    error: str | None = None


class BulkLifecycleResponse(BaseModel):
    requested: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    results: list[BulkLifecycleResultItem] = Field(default_factory=list)


class AccountBalanceResponse(BaseModel):
    """USDT balance snapshot for one strategy's sub-account.

    Drives the per-strategy budget card. ``error`` non-null means the adapter
    call failed; the dashboard treats those as 'balance unavailable' and
    falls back to local margin-used figures.
    """

    account_id: int
    exchange_name: str
    mode: str
    free_usdt: float | None
    total_usdt: float | None
    error: str | None = None
