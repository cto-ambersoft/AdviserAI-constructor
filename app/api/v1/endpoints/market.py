from typing import Any

from fastapi import APIRouter, Query

from app.schemas.market import (
    MARKET_BARS_DEFAULT,
    MARKET_BARS_MAX,
    MARKET_BARS_MIN,
    MARKET_COMMON_TIMEFRAMES,
    MARKET_EXCHANGE_DEFAULT,
    MARKET_SYMBOL_DEFAULT,
    MARKET_SYMBOL_MAX_LENGTH,
    MARKET_SYMBOL_MIN_LENGTH,
    MARKET_TIMEFRAME_DEFAULT,
    MARKET_TIMEFRAME_MAX_LENGTH,
    MARKET_TIMEFRAME_MIN_LENGTH,
    MarketMetaResponse,
    MarketOhlcvResponse,
)
from app.services.market_data.service import MarketDataService

router = APIRouter()
market_data = MarketDataService()


@router.get("/ohlcv", summary="Fetch OHLCV series")
async def get_ohlcv(
    exchange_name: str = Query(default=MARKET_EXCHANGE_DEFAULT, min_length=2, max_length=32),
    symbol: str = Query(
        default=MARKET_SYMBOL_DEFAULT,
        min_length=MARKET_SYMBOL_MIN_LENGTH,
        max_length=MARKET_SYMBOL_MAX_LENGTH,
    ),
    timeframe: str = Query(
        default=MARKET_TIMEFRAME_DEFAULT,
        min_length=MARKET_TIMEFRAME_MIN_LENGTH,
        max_length=MARKET_TIMEFRAME_MAX_LENGTH,
    ),
    bars: int = Query(default=MARKET_BARS_DEFAULT, ge=MARKET_BARS_MIN, le=MARKET_BARS_MAX),
) -> MarketOhlcvResponse:
    df = await market_data.fetch_ohlcv(
        exchange_name=exchange_name,
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
    )
    rows: list[dict[str, Any]] = [
        {str(key): value for key, value in row.items()}
        for row in df.reset_index().to_dict(orient="records")
    ]
    return MarketOhlcvResponse(
        exchange_name=exchange_name,
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
        rows=rows,
    )


@router.get("/meta", response_model=MarketMetaResponse, summary="Get market query metadata")
async def get_market_meta() -> MarketMetaResponse:
    return MarketMetaResponse(
        default_exchange_name=MARKET_EXCHANGE_DEFAULT,
        default_symbol=MARKET_SYMBOL_DEFAULT,
        symbol_min_length=MARKET_SYMBOL_MIN_LENGTH,
        symbol_max_length=MARKET_SYMBOL_MAX_LENGTH,
        default_timeframe=MARKET_TIMEFRAME_DEFAULT,
        timeframe_min_length=MARKET_TIMEFRAME_MIN_LENGTH,
        timeframe_max_length=MARKET_TIMEFRAME_MAX_LENGTH,
        common_timeframes=list(MARKET_COMMON_TIMEFRAMES),
        default_bars=MARKET_BARS_DEFAULT,
        min_bars=MARKET_BARS_MIN,
        max_bars=MARKET_BARS_MAX,
    )
