from typing import Any

import numpy as np
import pandas as pd


def _calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close_prev = (df["high"] - df["close"].shift(1)).abs()
    low_close_prev = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def atr_ob_backtest(
    df: pd.DataFrame,
    ema_period: int = 50,
    atr_period: int = 14,
    impulse_atr: float = 1.5,
    ob_buffer_atr: float = 0.15,
    tp_levels: list[tuple[float, float]] | None = None,
    one_trade_per_ob: bool = True,
    allocation_usdt: float = 1000.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if tp_levels is None:
        tp_levels = [(0.6, 0.30), (1.2, 0.40), (2.0, 0.30)]

    work = df.copy()
    work["EMA"] = _calc_ema(work["close"], ema_period)
    work["ATR"] = _calc_atr(work, atr_period)
    work["bull_low"] = np.nan
    work["bull_high"] = np.nan
    for i in range(2, len(work) - 2):
        atr = work["ATR"].iloc[i]
        if not np.isfinite(atr) or atr <= 0:
            continue
        bar = work.iloc[i]
        if (bar["high"] - bar["low"]) <= impulse_atr * atr:
            continue
        if bar["close"] < bar["open"]:
            work.loc[work.index[i + 1], ["bull_low", "bull_high"]] = (bar["low"], bar["high"])
    work[["bull_low", "bull_high"]] = work[["bull_low", "bull_high"]].ffill()

    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    used_ob: set[tuple[str, float, float]] = set()
    for i in range(1, len(work)):
        row = work.iloc[i]
        prev = work.iloc[i - 1]
        if position is None:
            has_ob = (
                np.isfinite(prev["bull_low"])
                and np.isfinite(prev["bull_high"])
                and np.isfinite(prev["ATR"])
            )
            is_bull_entry = (
                prev["bull_low"] < prev["close"] < prev["bull_high"] and prev["close"] > prev["EMA"]
            )
            if has_ob and is_bull_entry:
                ob_id = ("bull", float(prev["bull_low"]), float(prev["bull_high"]))
                if not one_trade_per_ob or ob_id not in used_ob:
                    entry = float(row["open"])
                    sl = float(prev["bull_low"] - ob_buffer_atr * prev["ATR"])
                    tps = [float(entry + mult * prev["ATR"]) for mult, _ in tp_levels]
                    fractions = [float(fr) for _, fr in tp_levels]
                    position = {
                        "entry_i": i,
                        "entry_time": work.index[i],
                        "entry": entry,
                        "sl": sl,
                        "tps": tps,
                        "fractions": fractions,
                        "tp_hits": [False] * len(tps),
                        "remaining": 1.0,
                        "pnl": 0.0,
                    }
                    used_ob.add(ob_id)
        else:
            if row["low"] <= position["sl"]:
                pnl_increment = (
                    (position["sl"] - position["entry"]) / position["entry"] * position["remaining"]
                )
                position["pnl"] += pnl_increment
                trades.append(
                    {
                        **position,
                        "exit_time": work.index[i],
                        "exit_i": i,
                        "exit_type": "SL",
                        "pnl_usdt": float(position["pnl"]) * float(allocation_usdt),
                        "allocation_usdt": float(allocation_usdt),
                    }
                )
                position = None
                continue
            for idx, take in enumerate(position["tps"]):
                if (not position["tp_hits"][idx]) and row["high"] >= take:
                    frac = position["fractions"][idx]
                    position["pnl"] += ((take - position["entry"]) / position["entry"]) * frac
                    position["remaining"] -= frac
                    position["tp_hits"][idx] = True
            if all(position["tp_hits"]):
                trades.append(
                    {
                        **position,
                        "exit_time": work.index[i],
                        "exit_i": i,
                        "exit_type": "TP",
                        "pnl_usdt": float(position["pnl"]) * float(allocation_usdt),
                        "allocation_usdt": float(allocation_usdt),
                    }
                )
                position = None
    return pd.DataFrame(trades), work


def run_atr_order_block(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    trades_df, work = atr_ob_backtest(
        df=df,
        ema_period=int(params.get("ema_period", 50)),
        atr_period=int(params.get("atr_period", 14)),
        impulse_atr=float(params.get("impulse_atr", 1.5)),
        ob_buffer_atr=float(params.get("ob_buffer_atr", 0.15)),
        tp_levels=params.get("tp_levels"),
        one_trade_per_ob=bool(params.get("one_trade_per_ob", True)),
        allocation_usdt=float(params.get("allocation_usdt", 1000.0)),
    )
    if trades_df.empty:
        summary = {"total_trades": 0, "win_rate": 0.0, "total_return_pct": 0.0}
        trades: list[dict[str, Any]] = []
    else:
        trades_df = trades_df.copy()
        trades_df["final_pnl"] = trades_df["pnl"].astype(float)
        summary = {
            "total_trades": int(len(trades_df)),
            "win_rate": float((trades_df["final_pnl"] > 0).mean() * 100),
            "total_return_pct": float(trades_df["final_pnl"].sum() * 100),
            "total_pnl_usdt": float(trades_df.get("pnl_usdt", pd.Series(dtype=float)).sum()),
        }
        raw_trades = trades_df.to_dict(orient="records")
        trades = [{str(key): value for key, value in row.items()} for row in raw_trades]
    return {
        "summary": summary,
        "trades": trades,
        "chart_points": {
            "ohlcv": work.reset_index().to_dict(orient="records"),
            "ema": work["EMA"].tolist(),
        },
        "explanations": [],
    }
