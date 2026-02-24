from typing import Any

from pydantic import BaseModel, Field

MARKET_SYMBOL_DEFAULT = "BTC/USDT"
MARKET_EXCHANGE_DEFAULT = "bybit"
MARKET_SYMBOL_MIN_LENGTH = 3
MARKET_SYMBOL_MAX_LENGTH = 24
MARKET_TIMEFRAME_DEFAULT = "1h"
MARKET_TIMEFRAME_MIN_LENGTH = 1
MARKET_TIMEFRAME_MAX_LENGTH = 16
MARKET_BARS_DEFAULT = 500
MARKET_BARS_MIN = 100
MARKET_BARS_MAX = 20_000
MARKET_COMMON_TIMEFRAMES = ("1m", "3m", "5m", "15m", "1h", "4h", "1d")


class MarketOhlcvResponse(BaseModel):
    exchange_name: str
    symbol: str
    timeframe: str
    bars: int
    rows: list[dict[str, Any]]


class MarketMetaResponse(BaseModel):
    default_exchange_name: str
    default_symbol: str
    symbol_min_length: int
    symbol_max_length: int
    default_timeframe: str
    timeframe_min_length: int
    timeframe_max_length: int
    common_timeframes: list[str]
    default_bars: int = Field(ge=1)
    min_bars: int = Field(ge=1)
    max_bars: int = Field(ge=1)
