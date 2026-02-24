import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.execution.base import ExchangeCredentials
from app.services.execution.factory import create_cex_adapter, normalize_exchange_name


class MarketDataService:
    def __init__(self) -> None:
        self._settings = get_settings()

    async def fetch_ohlcv(
        self,
        exchange_name: str,
        symbol: str,
        timeframe: str,
        bars: int,
        cache_ttl_seconds: int = 60,
    ) -> pd.DataFrame:
        normalized_exchange = normalize_exchange_name(exchange_name)
        cache_key = f"ohlcv:{normalized_exchange}:{symbol}:{timeframe}:{bars}"
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return cached

        adapter = create_cex_adapter(
            ExchangeCredentials(
                exchange_name=normalized_exchange,
                api_key="",
                api_secret="",
                mode="real",
            )
        )
        raw = await adapter.fetch_ohlcv(symbol=symbol, timeframe=timeframe, bars=bars)

        df = self._to_frame(raw)
        await self._set_cached(cache_key, df, cache_ttl_seconds)
        return df

    @staticmethod
    def frame_from_candles(candles: list[dict[str, Any]]) -> pd.DataFrame:
        df = pd.DataFrame(candles)
        if "time" not in df.columns:
            raise ValueError("Candles must include 'time'.")
        required_columns = {"open", "high", "low", "close", "volume"}
        missing = [column for column in required_columns if column not in df.columns]
        if missing:
            raise ValueError(f"Candles missing required columns: {', '.join(sorted(missing))}")

        parsed_time = pd.to_datetime(df["time"], utc=True, errors="coerce")
        if parsed_time.isna().any():
            bad_rows = parsed_time[parsed_time.isna()].index.tolist()[:5]
            raise ValueError(
                "Invalid candles.time values. Use ISO datetime or epoch-compatible values. "
                f"Bad row indexes: {bad_rows}."
            )
        df["time"] = parsed_time
        df = df.set_index("time").sort_index()
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    @staticmethod
    def _to_frame(raw: list[list[Any]]) -> pd.DataFrame:
        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        df = df.set_index("time").sort_index()
        return df.astype(float)

    async def _get_cached(self, key: str) -> pd.DataFrame | None:
        try:
            redis = Redis.from_url(self._settings.redis_url, decode_responses=True)
            payload = await redis.get(key)
            await redis.aclose()
        except Exception:
            return None
        if not payload:
            return None
        obj = json.loads(payload)
        rows = obj.get("rows", [])
        return self.frame_from_candles(rows)

    async def _set_cached(self, key: str, df: pd.DataFrame, ttl: int) -> None:
        def _index_to_utc_iso(index_value: object) -> str:
            ts = pd.to_datetime(str(index_value), utc=True)
            return ts.isoformat()

        rows = [
            {
                "time": _index_to_utc_iso(idx),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            for idx, row in df.iterrows()
        ]
        payload = json.dumps({"cached_at": datetime.now(UTC).isoformat(), "rows": rows})
        try:
            redis = Redis.from_url(self._settings.redis_url, decode_responses=True)
            await redis.set(key, payload, ex=ttl)
            await redis.aclose()
        except Exception:
            return
