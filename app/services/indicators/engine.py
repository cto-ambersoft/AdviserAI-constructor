from typing import Any, cast

import numpy as np
import pandas as pd
import pandas_ta as ta


def safe_series(value: object, index: pd.Index) -> pd.Series:
    if value is None:
        return pd.Series([np.nan] * len(index), index=index)
    if isinstance(value, pd.Series):
        return value.reindex(index)
    if isinstance(value, pd.DataFrame):
        return value.iloc[:, 0].reindex(index)
    return pd.Series(value, index=index)


def calc_indicators(df: pd.DataFrame) -> dict[str, pd.Series]:
    out: dict[str, pd.Series | None] = {}
    out["ema_fast"] = ta.ema(df["close"], 21)
    out["ema_slow"] = ta.ema(df["close"], 50)
    out["vwap"] = ta.vwap(
        df["high"],
        df["low"],
        df["close"],
        df["volume"],
    )
    out["rsi"] = ta.rsi(df["close"], 14)

    stoch = ta.stochrsi(df["close"], 14, 14, 3, 3)
    out["stoch_k"] = (
        stoch.iloc[:, 0] if isinstance(stoch, pd.DataFrame) and stoch.shape[1] >= 1 else None
    )
    out["stoch_d"] = (
        stoch.iloc[:, 1] if isinstance(stoch, pd.DataFrame) and stoch.shape[1] >= 2 else None
    )

    macd = ta.macd(df["close"], 12, 26, 9)
    out["macd_hist"] = (
        macd.iloc[:, 2] if isinstance(macd, pd.DataFrame) and macd.shape[1] >= 3 else None
    )
    out["macd_line"] = (
        macd.iloc[:, 0] if isinstance(macd, pd.DataFrame) and macd.shape[1] >= 1 else None
    )
    out["macd_signal"] = (
        macd.iloc[:, 1] if isinstance(macd, pd.DataFrame) and macd.shape[1] >= 2 else None
    )

    bb = ta.bbands(df["close"], 20, 2.0)
    bb_low = bb.iloc[:, 0] if isinstance(bb, pd.DataFrame) and bb.shape[1] >= 1 else None
    bb_mid = bb.iloc[:, 1] if isinstance(bb, pd.DataFrame) and bb.shape[1] >= 2 else None
    bb_high = bb.iloc[:, 2] if isinstance(bb, pd.DataFrame) and bb.shape[1] >= 3 else None
    out["bb_low"] = bb_low
    out["bb_mid"] = bb_mid
    out["bb_high"] = bb_high
    out["bb_width"] = (
        (bb_high - bb_low) / df["close"] if bb_low is not None and bb_high is not None else None
    )

    out["atr"] = ta.atr(df["high"], df["low"], df["close"], 14)
    adx = ta.adx(df["high"], df["low"], df["close"], 14)
    out["adx"] = (
        adx["ADX_14"] if isinstance(adx, pd.DataFrame) and "ADX_14" in adx.columns else None
    )
    out["vol_sma"] = ta.sma(df["volume"], 20)

    out["ichimoku_conversion"] = None
    out["ichimoku_base"] = None
    out["ichimoku_lead_a"] = None
    out["ichimoku_lead_b"] = None
    ichimoku_raw: Any = ta.ichimoku(df["high"], df["low"], df["close"])
    ichimoku_df: pd.DataFrame | None = None
    if isinstance(ichimoku_raw, tuple):
        if ichimoku_raw and isinstance(ichimoku_raw[0], pd.DataFrame):
            ichimoku_df = ichimoku_raw[0]
    elif isinstance(ichimoku_raw, pd.DataFrame):
        ichimoku_df = ichimoku_raw
    if ichimoku_df is not None:
        out["ichimoku_conversion"] = ichimoku_df.iloc[:, 0] if ichimoku_df.shape[1] >= 1 else None
        out["ichimoku_base"] = ichimoku_df.iloc[:, 1] if ichimoku_df.shape[1] >= 2 else None
        out["ichimoku_lead_a"] = ichimoku_df.iloc[:, 2] if ichimoku_df.shape[1] >= 3 else None
        out["ichimoku_lead_b"] = ichimoku_df.iloc[:, 3] if ichimoku_df.shape[1] >= 4 else None

    out["supertrend"] = None
    out["supertrend_upper"] = None
    out["supertrend_lower"] = None
    supertrend_raw: Any = ta.supertrend(df["high"], df["low"], df["close"], 10, 3)
    if isinstance(supertrend_raw, pd.DataFrame):
        out["supertrend"] = supertrend_raw.iloc[:, 0] if supertrend_raw.shape[1] >= 1 else None
        out["supertrend_upper"] = (
            supertrend_raw.iloc[:, 1] if supertrend_raw.shape[1] >= 2 else None
        )
        out["supertrend_lower"] = (
            supertrend_raw.iloc[:, 2] if supertrend_raw.shape[1] >= 3 else None
        )

    try:
        pivot = cast(Any, ta).pivots(df["high"], df["low"], df["close"], 5)
    except Exception:
        pivot = None
    if isinstance(pivot, pd.DataFrame) and pivot.shape[1] >= 3:
        out["pivot_p"] = pivot.iloc[:, 0]
        out["pivot_s1"] = pivot.iloc[:, 1]
        out["pivot_r1"] = pivot.iloc[:, 2]
    else:
        out["pivot_p"] = None
        out["pivot_s1"] = None
        out["pivot_r1"] = None

    out["cci"] = ta.cci(df["high"], df["low"], df["close"], 20)
    out["willr"] = ta.willr(df["high"], df["low"], df["close"], 14)

    return {key: safe_series(val, df.index) for key, val in out.items()}
