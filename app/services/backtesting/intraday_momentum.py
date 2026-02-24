from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta

from app.services.backtesting.common import PositionSizer


def intraday_momentum_backtest(
    df: pd.DataFrame,
    lookback: int = 20,
    atr_period: int = 14,
    atr_mult: float = 2.0,
    rr: float = 2.0,
    vol_sma: int = 20,
    vol_mult: float = 1.2,
    time_exit_bars: int = 48,
    side: str = "long",
    allocation_usdt: float = 1000.0,
    risk_per_trade_pct: float = 1.0,
    max_positions: int = 1,
    fee_pct: float = 0.06,
    entry_size_usdt: float | None = None,
) -> pd.DataFrame:
    work = df.copy()
    work["atr"] = ta.atr(work["high"], work["low"], work["close"], length=atr_period)
    work["donch_hi"] = work["high"].rolling(lookback).max().shift(1)
    work["donch_lo"] = work["low"].rolling(lookback).min().shift(1)
    work["vol_sma"] = work["volume"].rolling(vol_sma).mean()
    sizer = PositionSizer(
        account_balance=allocation_usdt,
        risk_per_trade=risk_per_trade_pct,
        max_open_positions=max_positions,
        max_position_pct=100.0,
    )
    in_position = False
    entry_i = -1
    entry = sl = tp = qty = 0.0
    trades: list[dict[str, Any]] = []
    fee = fee_pct / 100.0
    start = max(lookback, atr_period, vol_sma) + 2
    for i in range(start, len(work) - 1):
        row = work.iloc[i]
        nxt = work.iloc[i + 1]
        if not np.isfinite(row["atr"]) or row["atr"] <= 0:
            continue
        if not in_position:
            if side.startswith("l"):
                cond = row["close"] > row["donch_hi"] and row["volume"] > row["vol_sma"] * vol_mult
                if cond:
                    entry_i = i + 1
                    entry = float(nxt["open"])
                    sl = float(entry - row["atr"] * atr_mult)
                    tp = float(entry + (entry - sl) * rr)
                    if entry_size_usdt is not None:
                        position_value = min(float(entry_size_usdt), float(allocation_usdt))
                        qty = position_value / entry if entry > 0 else 0.0
                        in_position = qty > 0
                    else:
                        sizing = sizer.calculate_position_size(entry, sl)
                        if bool(sizing["allowed"]) and float(sizing["quantity"]) > 0:
                            qty = float(sizing["quantity"])
                            in_position = True
            else:
                cond = row["close"] < row["donch_lo"] and row["volume"] > row["vol_sma"] * vol_mult
                if cond:
                    entry_i = i + 1
                    entry = float(nxt["open"])
                    sl = float(entry + row["atr"] * atr_mult)
                    tp = float(entry - (sl - entry) * rr)
                    if entry_size_usdt is not None:
                        position_value = min(float(entry_size_usdt), float(allocation_usdt))
                        qty = position_value / entry if entry > 0 else 0.0
                        in_position = qty > 0
                    else:
                        sizing = sizer.calculate_position_size(entry, sl)
                        if bool(sizing["allowed"]) and float(sizing["quantity"]) > 0:
                            qty = float(sizing["quantity"])
                            in_position = True
            continue

        high = float(row["high"])
        low = float(row["low"])
        exit_reason: str | None = None
        exit_price = 0.0
        age = i - entry_i
        if side.startswith("l"):
            if low <= sl:
                exit_price, exit_reason = sl, "STOP"
            elif high >= tp:
                exit_price, exit_reason = tp, "TAKE"
        else:
            if high >= sl:
                exit_price, exit_reason = sl, "STOP"
            elif low <= tp:
                exit_price, exit_reason = tp, "TAKE"
        if exit_reason is None and age >= time_exit_bars:
            exit_price, exit_reason = float(row["close"]), "TIME"
        if exit_reason is None:
            continue
        gross = (exit_price - entry) * qty if side.startswith("l") else (entry - exit_price) * qty
        fees = (entry * qty + exit_price * qty) * fee
        pnl = gross - fees
        trades.append(
            {
                "strategy": "Intraday Momentum",
                "side": "LONG" if side.startswith("l") else "SHORT",
                "entry_time": work.index[entry_i],
                "exit_time": work.index[i],
                "entry": entry,
                "exit": exit_price,
                "sl": sl,
                "tp": tp,
                "qty": qty,
                "pnl_usdt": pnl,
                "pnl_pct": pnl / allocation_usdt if allocation_usdt > 0 else np.nan,
                "exit_reason": exit_reason,
            }
        )
        in_position = False
    return pd.DataFrame(trades)


def run_intraday_momentum(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    trades_df = intraday_momentum_backtest(
        df=df,
        lookback=int(params.get("lookback", 20)),
        atr_period=int(params.get("atr_period", 14)),
        atr_mult=float(params.get("atr_mult", 2.0)),
        rr=float(params.get("rr", 2.0)),
        vol_sma=int(params.get("vol_sma", 20)),
        vol_mult=float(params.get("vol_mult", 1.2)),
        time_exit_bars=int(params.get("time_exit_bars", 48)),
        side=str(params.get("side", "long")),
        allocation_usdt=float(params.get("allocation_usdt", 1000.0)),
        risk_per_trade_pct=float(params.get("risk_per_trade_pct", 1.0)),
        max_positions=int(params.get("max_positions", 1)),
        fee_pct=float(params.get("fee_pct", 0.06)),
        entry_size_usdt=(
            float(params["entry_size_usdt"])
            if params.get("entry_size_usdt") is not None
            else None
        ),
    )
    if trades_df.empty:
        summary = {"total_trades": 0, "win_rate": 0.0, "total_pnl_usdt": 0.0}
    else:
        summary = {
            "total_trades": int(len(trades_df)),
            "win_rate": float((trades_df["pnl_usdt"] > 0).mean() * 100),
            "total_pnl_usdt": float(trades_df["pnl_usdt"].sum()),
        }
    return {
        "summary": summary,
        "trades": trades_df.to_dict(orient="records"),
        "chart_points": {"ohlcv": df.reset_index().to_dict(orient="records")},
        "explanations": [],
    }
