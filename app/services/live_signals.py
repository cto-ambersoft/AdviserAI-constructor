from typing import Any

import numpy as np
import pandas as pd

from app.services.backtesting.common import PositionSizer
from app.services.backtesting.stop_logic import compute_stop_loss
from app.services.backtesting.vwap_builder import (
    compute_indicator_snapshot,
    long_conditions,
    short_conditions,
)
from app.services.indicators.engine import calc_indicators
from app.services.market_data.service import MarketDataService


def _safe_last_closed_index(df: pd.DataFrame) -> int:
    if df is None or df.empty or len(df) < 3:
        return -1
    return len(df) - 2


def _calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close_prev = (df["high"] - df["close"].shift(1)).abs()
    low_close_prev = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
    return tr.rolling(period).mean()


class LiveSignalService:
    def __init__(self, market_data: MarketDataService | None = None) -> None:
        self._market_data = market_data or MarketDataService()

    async def load_market_frame(
        self,
        *,
        symbol: str,
        timeframe: str,
        bars: int,
        candles: list[dict[str, Any]] | None,
    ) -> pd.DataFrame:
        if candles:
            return self._market_data.frame_from_candles(candles)
        return await self._market_data.fetch_ohlcv(
            exchange_name="bybit",
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
        )

    async def compute_builder_signal(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self.load_market_frame(
            symbol=str(payload["symbol"]),
            timeframe=str(payload["timeframe"]),
            bars=int(payload["bars"]),
            candles=payload.get("candles"),
        )
        ind = calc_indicators(df)
        idx = _safe_last_closed_index(df)
        if idx < 0:
            return {"has_signal": False, "reasons": ["Not enough bars"]}
        snap = compute_indicator_snapshot(df.iloc[idx], ind, idx)
        enabled = {str(item) for item in payload.get("enabled", [])}
        regime = str(payload.get("regime", "Flat"))
        long_ok, long_reasons = long_conditions(snap, enabled, regime)
        short_ok, short_reasons = short_conditions(snap, enabled, regime)
        side = None
        reasons: list[str] = []
        if long_ok and not short_ok:
            side, reasons = "LONG", long_reasons
        elif short_ok and not long_ok:
            side, reasons = "SHORT", short_reasons
        elif long_ok and short_ok:
            side, reasons = ("LONG", long_reasons) if regime != "Bear" else ("SHORT", short_reasons)
        if side is None:
            return {"has_signal": False, "bar_time": str(df.index[idx]), "reasons": []}
        entry = float(snap["close"])
        sl, sl_explain = compute_stop_loss(
            df=df,
            indicators=ind,
            idx=idx,
            side=side,
            entry=entry,
            atr_mult=float(payload.get("atr_mult", 1.5)),
            stop_mode=str(payload.get("stop_mode", "ATR")),
            swing_lookback=int(payload.get("swing_lookback", 20)),
            swing_buffer_atr=float(payload.get("swing_buffer_atr", 0.3)),
            ob_impulse_atr=float(payload.get("ob_impulse_atr", 1.5)),
            ob_buffer_atr=float(payload.get("ob_buffer_atr", 0.15)),
            ob_lookback=int(payload.get("ob_lookback", 120)),
        )
        rr = float(payload.get("rr", 2.0))
        tp = entry + (entry - sl) * rr if side == "LONG" else entry - (sl - entry) * rr
        sizer = PositionSizer(
            account_balance=float(payload.get("account_balance", 1000.0)),
            risk_per_trade=float(payload.get("risk_per_trade", 1.0)),
            max_open_positions=int(payload.get("max_positions", 1)),
            max_position_pct=float(payload.get("max_position_pct", 100.0)),
        )
        sizing = sizer.calculate_position_size(entry, sl)
        return {
            "has_signal": True,
            "side": side,
            "entry": entry,
            "sl": float(sl),
            "tp": float(tp),
            "bar_time": str(df.index[idx]),
            "reasons": reasons,
            "sizing": sizing,
            "sl_explain": sl_explain,
        }

    async def compute_atr_ob_signal(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self.load_market_frame(
            symbol=str(payload["symbol"]),
            timeframe=str(payload["timeframe"]),
            bars=int(payload["bars"]),
            candles=payload.get("candles"),
        )
        work = df.copy()
        ema_period = int(payload.get("ema_period", 50))
        atr_period = int(payload.get("atr_period", 14))
        impulse_atr = float(payload.get("impulse_atr", 1.5))
        ob_buffer_atr = float(payload.get("ob_buffer_atr", 0.15))
        work["EMA"] = _calc_ema(work["close"], ema_period)
        work["ATR"] = _calc_atr(work, atr_period)
        work["bull_low"] = np.nan
        work["bull_high"] = np.nan
        for k in range(2, len(work) - 2):
            atr = work["ATR"].iloc[k]
            if not np.isfinite(atr) or atr <= 0:
                continue
            bar = work.iloc[k]
            rng = float(bar["high"] - bar["low"])
            if rng <= impulse_atr * float(atr):
                continue
            if float(bar["close"]) < float(bar["open"]):
                work.loc[work.index[k + 1], "bull_low"] = float(bar["low"])
                work.loc[work.index[k + 1], "bull_high"] = float(bar["high"])

        idx = _safe_last_closed_index(work)
        if idx < 2:
            return {"has_signal": False, "reasons": ["Not enough bars"]}
        prev = work.iloc[idx - 1]
        cur = work.iloc[idx]
        ob_low = prev["bull_low"]
        ob_high = prev["bull_high"]
        if not (np.isfinite(ob_low) and np.isfinite(ob_high)):
            return {"has_signal": False, "bar_time": str(work.index[idx]), "reasons": []}
        prev_close = float(prev["close"])
        if not (float(ob_low) <= prev_close <= float(ob_high)):
            return {"has_signal": False, "bar_time": str(work.index[idx]), "reasons": []}
        if prev_close <= float(prev["EMA"]):
            return {"has_signal": False, "bar_time": str(work.index[idx]), "reasons": []}
        atr = float(cur["ATR"]) if np.isfinite(cur["ATR"]) else np.nan
        if not np.isfinite(atr) or atr <= 0:
            return {"has_signal": False, "bar_time": str(work.index[idx]), "reasons": ["ATR missing"]}
        entry = float(cur["open"])
        sl = float(ob_low) - ob_buffer_atr * atr
        tp = entry + 0.6 * atr
        return {
            "has_signal": True,
            "side": "LONG",
            "entry": entry,
            "sl": float(sl),
            "tp": float(tp),
            "bar_time": str(work.index[idx]),
            "reasons": [
                f"Prev close inside OB [{float(ob_low):.2f}..{float(ob_high):.2f}]",
                f"Prev close > EMA{ema_period}",
            ],
            "sizing": {"allowed": True, "position_value": float(payload.get('allocation_usdt', 1000.0))},
            "sl_explain": {
                "mode": "Order Block (ATR-OB)",
                "ob_low": float(ob_low),
                "ob_high": float(ob_high),
                "atr": float(atr),
                "buffer_atr": float(ob_buffer_atr),
            },
        }
