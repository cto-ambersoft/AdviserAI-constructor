from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.market import MARKET_EXCHANGE_DEFAULT


class SignalBaseRequest(BaseModel):
    exchange_name: str = Field(default=MARKET_EXCHANGE_DEFAULT, min_length=2, max_length=32)
    symbol: str = Field(default="BTC/USDT", min_length=3, max_length=24)
    timeframe: str = Field(default="1h", min_length=1, max_length=16)
    bars: int = Field(default=500, ge=100, le=20_000)
    candles: list[dict[str, Any]] | None = None


class BuilderSignalRequest(SignalBaseRequest):
    enabled: list[str] = Field(default_factory=list)
    regime: Literal["Bull", "Flat", "Bear"] = "Flat"
    rr: float = Field(default=2.0, ge=0.5, le=10.0)
    atr_mult: float = Field(default=1.5, ge=0.1, le=10.0)
    account_balance: float = Field(default=1000.0, gt=0)
    risk_per_trade: float = Field(default=1.0, gt=0, le=10.0)
    max_positions: int = Field(default=1, ge=1, le=50)
    max_position_pct: float = Field(default=100.0, gt=0, le=100.0)
    stop_mode: Literal["ATR", "Swing", "Order Block (ATR-OB)"] = "ATR"
    swing_lookback: int = Field(default=20, ge=2, le=500)
    swing_buffer_atr: float = Field(default=0.3, ge=0, le=10.0)
    ob_impulse_atr: float = Field(default=1.5, ge=0.1, le=10.0)
    ob_buffer_atr: float = Field(default=0.15, ge=0, le=10.0)
    ob_lookback: int = Field(default=120, ge=5, le=5000)


class AtrObSignalRequest(SignalBaseRequest):
    ema_period: int = Field(default=50, ge=5, le=500)
    atr_period: int = Field(default=14, ge=5, le=200)
    impulse_atr: float = Field(default=1.5, ge=0.1, le=10.0)
    ob_buffer_atr: float = Field(default=0.15, ge=0, le=10.0)
    allocation_usdt: float = Field(default=1000.0, gt=0)


class SignalExecuteRequest(BaseModel):
    mode: Literal["dry_run", "live"] = "dry_run"
    execute: bool = False
    account_id: int | None = Field(default=None, ge=1)
    fee_pct: float = Field(default=0.06, ge=0, le=1.0)

    @model_validator(mode="after")
    def validate_live_requirements(self) -> "SignalExecuteRequest":
        if self.mode == "live" and self.execute and self.account_id is None:
            raise ValueError("account_id is required when executing in live mode")
        return self


class LiveSignalResult(BaseModel):
    has_signal: bool
    side: str | None = None
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    bar_time: str | None = None
    reasons: list[str] = Field(default_factory=list)
    sizing: dict[str, Any] = Field(default_factory=dict)
    sl_explain: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)


class BuilderSignalRunRequest(BaseModel):
    signal: BuilderSignalRequest
    execution: SignalExecuteRequest = Field(default_factory=SignalExecuteRequest)


class AtrObSignalRunRequest(BaseModel):
    signal: AtrObSignalRequest
    execution: SignalExecuteRequest = Field(default_factory=SignalExecuteRequest)


class LivePaperProfileUpsertRequest(BaseModel):
    strategy_id: int = Field(gt=0)
    total_balance_usdt: float = Field(gt=0)
    per_trade_usdt: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_position_notional(self) -> "LivePaperProfileUpsertRequest":
        if self.per_trade_usdt > self.total_balance_usdt:
            raise ValueError("per_trade_usdt must be less than or equal to total_balance_usdt.")
        return self


class LivePaperProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    strategy_id: int
    strategy_revision: int
    is_running: bool
    total_balance_usdt: float
    per_trade_usdt: float
    last_processed_at: datetime | None = None
    last_poll_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LivePaperTradeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    strategy_id: int
    strategy_revision: int
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    pnl_usdt: float
    status: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class LivePaperEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    strategy_revision: int
    event_type: str
    event_time: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class LivePaperPlayStopResponse(BaseModel):
    profile: LivePaperProfileRead


class LivePaperPollResponse(BaseModel):
    profile: LivePaperProfileRead
    live_trades_since_start: list[LivePaperTradeRead] = Field(default_factory=list)
    events: list[LivePaperEventRead] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
