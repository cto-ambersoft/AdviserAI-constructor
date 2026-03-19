import math
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class AutoTradeConfigUpsertRequest(BaseModel):
    enabled: bool = False
    profile_id: int = Field(gt=0)
    account_id: int = Field(gt=0)
    position_size_usdt: float = Field(gt=0)
    leverage: int = Field(default=1, ge=1, le=125)
    min_confidence_pct: float = Field(default=62.0, ge=0, le=100)
    fast_close_confidence_pct: float = Field(default=80.0, ge=0, le=100)
    confirm_reports_required: int = Field(default=2, ge=1, le=5)
    risk_mode: RiskMode = "1:2"
    sl_pct: float = Field(gt=0)
    tp_pct: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_risk_mode_ratio(self) -> "AutoTradeConfigUpsertRequest":
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
    last_started_at: datetime | None
    last_stopped_at: datetime | None
    created_at: datetime
    updated_at: datetime


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


class AutoTradePlayStopResponse(BaseModel):
    config: AutoTradeConfigRead


class AutoTradeStateResponse(BaseModel):
    config: AutoTradeConfigRead | None = None


class AutoTradeEventsResponse(BaseModel):
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
