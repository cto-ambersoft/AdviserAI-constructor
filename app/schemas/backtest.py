from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

VWAP_ALLOWED_INDICATORS = {
    "EMA Fast (21)",
    "EMA Slow (50)",
    "VWAP",
    "RSI",
    "Stoch RSI",
    "MACD",
    "Bollinger Bands",
    "BB Width",
    "ATR",
    "ADX",
    "Volume SMA",
    "Ichimoku",
    "Supertrend",
    "Pivot Points",
    "CCI",
    "Williams %R",
}
VWAP_ALLOWED_PRESETS = (
    "Custom",
    "Trend",
    "Range",
    "Breakdown",
    "Advanced Ichimoku",
    "Pivots+CCI",
)
VWAP_ALLOWED_REGIMES = ("Bull", "Flat", "Bear")
VWAP_STOP_MODES = ("ATR", "Swing", "Order Block (ATR-OB)")
VWAP_TIMEFRAMES = ("15m", "1h", "4h")
ATR_ORDER_BLOCK_TIMEFRAMES = ("15m", "1h", "4h", "1d")
KNIFE_CATCHER_TIMEFRAMES = ("1m", "3m", "5m", "15m", "1h")
KNIFE_CATCHER_SIDES = ("long", "short")
KNIFE_CATCHER_ENTRY_MODE_LONG = ("OPEN_LOW", "HIGH_LOW")
KNIFE_CATCHER_ENTRY_MODE_SHORT = ("OPEN_HIGH", "LOW_HIGH")
GRID_BOT_TIMEFRAMES = ("5m", "15m", "1h", "4h")
INTRADAY_MOMENTUM_TIMEFRAMES = ("5m", "15m", "1h", "4h")
INTRADAY_MOMENTUM_SIDES = ("long", "short")
PORTFOLIO_TIMEFRAMES = ("15m", "1h", "4h", "1d")
PORTFOLIO_BUILTIN_STRATEGIES = (
    "VWAP Builder",
    "ATR Order-Block",
    "Knife Catcher",
    "Grid BOT",
    "Intraday Momentum",
)


class CandleInput(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class BaseBacktestRequest(BaseModel):
    symbol: str = Field(default="BTC/USDT", min_length=3, max_length=24)
    timeframe: str = Field(default="1h", min_length=1, max_length=16)
    bars: int = Field(default=500, ge=100, le=20_000)
    candles: list[CandleInput] | None = Field(
        default=None,
        description=(
            "Optional OHLCV override from client. "
            "If omitted, backend fetches candles by symbol/timeframe/bars."
        ),
    )
    include_series: bool = True
    trades_limit: int = Field(default=1000, ge=10, le=10_000)


class VwapBacktestRequest(BaseBacktestRequest):
    regime: Literal["Bull", "Flat", "Bear"] = Field(default="Flat")
    preset: Literal[
        "Custom",
        "Trend",
        "Range",
        "Breakdown",
        "Advanced Ichimoku",
        "Pivots+CCI",
    ] = Field(default="Custom")
    enabled: list[str] = Field(
        default_factory=list,
        description=(
            "Selected indicators for VWAP builder. If empty, backend uses indicators from preset."
        ),
    )
    rr: float = Field(default=2.0, ge=0.5, le=10.0)
    atr_mult: float = Field(default=1.5, ge=0.1, le=10.0)
    cooldown_bars: int = Field(default=5, ge=0, le=100)
    account_balance: float = Field(default=1000.0, gt=0)
    risk_per_trade: float = Field(default=1.0, gt=0, le=10.0)
    max_positions: int = Field(default=5, ge=1, le=50)
    max_position_pct: float = Field(default=100.0, gt=0, le=100.0)
    stop_mode: Literal["ATR", "Swing", "Order Block (ATR-OB)"] = Field(default="ATR")
    swing_lookback: int = Field(default=20, ge=2, le=500)
    swing_buffer_atr: float = Field(default=0.3, ge=0, le=10.0)
    ob_impulse_atr: float = Field(default=1.5, ge=0.1, le=10.0)
    ob_buffer_atr: float = Field(default=0.15, ge=0, le=10.0)
    ob_lookback: int = Field(default=120, ge=5, le=5000)

    @field_validator("enabled")
    @classmethod
    def validate_enabled(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - VWAP_ALLOWED_INDICATORS)
        if unknown:
            raise ValueError(f"Unknown indicators: {', '.join(unknown)}")
        return value


class AtrOrderBlockRequest(BaseBacktestRequest):
    ema_period: int = 50
    atr_period: int = 14
    impulse_atr: float = 1.5
    ob_buffer_atr: float = 0.15
    tp_levels: list[tuple[float, float]] | None = None
    one_trade_per_ob: bool = True
    allocation_usdt: float = Field(default=1000.0, gt=0)


class KnifeCatcherRequest(BaseBacktestRequest):
    side: str = "long"
    entry_mode_long: str = "OPEN_LOW"
    entry_mode_short: str = "OPEN_HIGH"
    knife_move_pct: float = 0.35
    entry_k_pct: float = 65.0
    tp_pct: float = 0.45
    sl_pct: float = 0.35
    use_max_range_filter: bool = True
    max_range_pct: float = 1.2
    use_wick_filter: bool = True
    max_wick_share_pct: float = 65.0
    requote_each_candle: bool = True
    max_requotes: int = 6
    account_balance: float = Field(default=1000.0, gt=0)


class GridBotRequest(BaseBacktestRequest):
    ma_period: int = 50
    grid_spacing_pct: float = 0.5
    grids_down: int = 8
    order_fee_pct: float = 0.06
    allocation_usdt: float = 1000.0
    initial_capital_usdt: float | None = Field(default=None, gt=0)
    order_size_usdt: float | None = Field(default=None, gt=0)
    close_open_positions_on_eod: bool = True


class IntradayMomentumRequest(BaseBacktestRequest):
    lookback: int = 20
    atr_period: int = 14
    atr_mult: float = 2.0
    rr: float = 2.0
    vol_sma: int = 20
    vol_mult: float = 1.2
    time_exit_bars: int = 48
    side: str = "long"
    allocation_usdt: float = 1000.0
    risk_per_trade_pct: float = 1.0
    max_positions: int = 1
    fee_pct: float = 0.06
    entry_size_usdt: float | None = Field(default=None, gt=0)


class PortfolioStrategyInput(BaseModel):
    name: str
    weight: float = Field(default=0.0, ge=0)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class PortfolioUserStrategyInput(BaseModel):
    strategy_id: int = Field(gt=0)
    allocation_pct: float = Field(default=0.0, ge=0.0, le=100.0)


class PortfolioBuiltinStrategyInput(BaseModel):
    name: str
    allocation_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_builtin_name(cls, value: str) -> str:
        if value not in PORTFOLIO_BUILTIN_STRATEGIES:
            supported = ", ".join(PORTFOLIO_BUILTIN_STRATEGIES)
            raise ValueError(f"Unsupported builtin strategy '{value}'. Supported: {supported}")
        return value


class PortfolioBacktestRequest(BaseModel):
    total_capital: float = Field(default=5000.0, gt=0)
    user_strategies: list[PortfolioUserStrategyInput] = Field(default_factory=list)
    builtin_strategies: list[PortfolioBuiltinStrategyInput] = Field(default_factory=list)
    # Backward-compatible legacy payload path.
    strategies: list[PortfolioStrategyInput] = Field(default_factory=list)
    async_job: bool = False

    @model_validator(mode="after")
    def validate_allocations(self) -> "PortfolioBacktestRequest":
        if self.strategies:
            return self

        merged_allocations = [item.allocation_pct for item in self.user_strategies] + [
            item.allocation_pct for item in self.builtin_strategies
        ]
        if not merged_allocations:
            return self
        if sum(merged_allocations) <= 0:
            raise ValueError("At least one selected strategy must have allocation_pct > 0.")
        return self


class BacktestResponse(BaseModel):
    summary: dict[str, Any]
    trades: list[dict[str, Any]]
    chart_points: dict[str, Any]
    explanations: list[Any]


class VwapCatalog(BaseModel):
    timeframes: list[str]
    presets: list[str]
    regimes: list[str]
    indicators: list[str]
    stop_modes: list[str]


class AtrOrderBlockCatalog(BaseModel):
    timeframes: list[str]


class KnifeCatcherCatalog(BaseModel):
    timeframes: list[str]
    sides: list[str]
    entry_mode_long: list[str]
    entry_mode_short: list[str]


class GridBotCatalog(BaseModel):
    timeframes: list[str]


class IntradayMomentumCatalog(BaseModel):
    timeframes: list[str]
    sides: list[str]


class PortfolioCatalog(BaseModel):
    timeframes: list[str]
    builtin_strategies: list[str]
    builtin_strategy_params: dict[str, list[str]] = Field(default_factory=dict)


class BacktestCatalogResponse(BaseModel):
    vwap: VwapCatalog
    atr_order_block: AtrOrderBlockCatalog
    knife_catcher: KnifeCatcherCatalog
    grid_bot: GridBotCatalog
    intraday_momentum: IntradayMomentumCatalog
    portfolio: PortfolioCatalog
