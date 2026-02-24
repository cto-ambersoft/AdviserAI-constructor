from typing import Any

import numpy as np
import pandas as pd


def find_last_order_block(
    df: pd.DataFrame,
    atr_series: pd.Series,
    idx: int,
    side: str,
    impulse_atr: float,
    lookback: int,
) -> dict[str, float | int] | None:
    """Find last impulse bar to derive OB range for stop placement."""
    lb = max(5, int(lookback))
    start = max(0, idx - lb)
    for k in range(idx - 1, start - 1, -1):
        atr_k = float(atr_series.iloc[k]) if k < len(atr_series) else float("nan")
        if not np.isfinite(atr_k) or atr_k <= 0:
            continue
        rng = float(df["high"].iloc[k] - df["low"].iloc[k])
        if rng <= float(impulse_atr) * atr_k:
            continue
        open_price = float(df["open"].iloc[k])
        close_price = float(df["close"].iloc[k])
        if side == "LONG" and close_price < open_price:
            return {
                "ob_low": float(df["low"].iloc[k]),
                "ob_high": float(df["high"].iloc[k]),
                "bar_i": int(k),
            }
        if side == "SHORT" and close_price > open_price:
            return {
                "ob_low": float(df["low"].iloc[k]),
                "ob_high": float(df["high"].iloc[k]),
                "bar_i": int(k),
            }
    return None


def compute_stop_loss(
    *,
    df: pd.DataFrame,
    indicators: dict[str, Any],
    idx: int,
    side: str,
    entry: float,
    atr_mult: float,
    stop_mode: str,
    swing_lookback: int,
    swing_buffer_atr: float,
    ob_impulse_atr: float,
    ob_buffer_atr: float,
    ob_lookback: int,
) -> tuple[float, dict[str, Any]]:
    """Compute stop loss and explanation payload for strategy analytics."""
    entry_value = float(entry)
    atr_series = indicators.get("atr")
    atr = float(atr_series.iloc[idx]) if atr_series is not None else float("nan")
    atr = atr if np.isfinite(atr) else float("nan")
    mode = (stop_mode or "ATR").strip()

    if mode == "ATR":
        dist = atr * float(atr_mult) if np.isfinite(atr) else float("nan")
        stop = (entry_value - dist) if side == "LONG" else (entry_value + dist)
        return float(stop), {
            "mode": "ATR",
            "atr": float(atr) if np.isfinite(atr) else np.nan,
            "atr_mult": float(atr_mult),
            "distance": float(dist) if np.isfinite(dist) else np.nan,
            "formula": "SL = Entry - ATR×mult" if side == "LONG" else "SL = Entry + ATR×mult",
        }

    if mode == "Swing":
        lb = max(2, int(swing_lookback))
        start = max(0, idx - lb)
        window = df.iloc[start : idx + 1]
        swing_low = float(window["low"].min())
        swing_high = float(window["high"].max())
        buffer_abs = float(swing_buffer_atr) * atr if np.isfinite(atr) else 0.0
        if side == "LONG":
            stop = swing_low - buffer_abs
            base = swing_low
            formula = "SL = SwingLow - ATR×buffer"
        else:
            stop = swing_high + buffer_abs
            base = swing_high
            formula = "SL = SwingHigh + ATR×buffer"
        return float(stop), {
            "mode": "Swing",
            "lookback": int(lb),
            "swing_low": float(swing_low),
            "swing_high": float(swing_high),
            "atr": float(atr) if np.isfinite(atr) else np.nan,
            "buffer_atr": float(swing_buffer_atr),
            "buffer_abs": float(buffer_abs),
            "base_level": float(base),
            "formula": formula,
        }

    if mode == "Order Block (ATR-OB)":
        order_block = (
            find_last_order_block(df, atr_series, idx, side, ob_impulse_atr, ob_lookback)
            if atr_series is not None
            else None
        )
        if order_block is not None and np.isfinite(atr):
            buffer_abs = float(ob_buffer_atr) * atr
            if side == "LONG":
                stop = float(order_block["ob_low"]) - buffer_abs
                base = float(order_block["ob_low"])
                formula = "SL = OB.low - ATR×buffer"
            else:
                stop = float(order_block["ob_high"]) + buffer_abs
                base = float(order_block["ob_high"])
                formula = "SL = OB.high + ATR×buffer"
            return float(stop), {
                "mode": "Order Block (ATR-OB)",
                "atr": float(atr),
                "impulse_atr": float(ob_impulse_atr),
                "lookback": int(ob_lookback),
                "ob_low": float(order_block["ob_low"]),
                "ob_high": float(order_block["ob_high"]),
                "ob_bar_i": int(order_block["bar_i"]),
                "buffer_atr": float(ob_buffer_atr),
                "buffer_abs": float(buffer_abs),
                "base_level": float(base),
                "formula": formula,
            }
        dist = atr * float(atr_mult) if np.isfinite(atr) else float("nan")
        stop = (entry_value - dist) if side == "LONG" else (entry_value + dist)
        return float(stop), {
            "mode": "Order Block (ATR-OB)",
            "fallback": "ATR",
            "atr": float(atr) if np.isfinite(atr) else np.nan,
            "atr_mult": float(atr_mult),
            "distance": float(dist) if np.isfinite(dist) else np.nan,
            "formula": "SL = Entry - ATR×mult" if side == "LONG" else "SL = Entry + ATR×mult",
            "note": "Order Block not found in lookback, fallback to ATR.",
        }

    dist = atr * float(atr_mult) if np.isfinite(atr) else float("nan")
    stop = (entry_value - dist) if side == "LONG" else (entry_value + dist)
    return float(stop), {
        "mode": "ATR",
        "atr": float(atr) if np.isfinite(atr) else np.nan,
        "atr_mult": float(atr_mult),
        "distance": float(dist) if np.isfinite(dist) else np.nan,
        "formula": "SL = Entry - ATR×mult" if side == "LONG" else "SL = Entry + ATR×mult",
        "note": f"Unknown stop_mode={mode!r}, fallback to ATR.",
    }
