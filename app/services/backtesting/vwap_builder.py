from typing import Any

import numpy as np
import pandas as pd

from app.services.backtesting.common import (
    PositionSizer,
    add_capital_metrics,
    annotate_trade_confirmations,
    calculate_performance_metrics,
)
from app.services.backtesting.stop_logic import compute_stop_loss

VWAP_PRESETS = {
    "Custom",
    "Trend",
    "Range",
    "Breakdown",
    "Advanced Ichimoku",
    "Pivots+CCI",
}


def apply_preset(preset: str) -> list[str]:
    if preset == "Trend":
        return ["EMA Slow (50)", "EMA Fast (21)", "VWAP", "MACD", "ADX", "ATR"]
    if preset == "Range":
        return ["Bollinger Bands", "BB Width", "RSI", "Stoch RSI", "ATR"]
    if preset == "Breakdown":
        return ["VWAP", "MACD", "ADX", "ATR", "Volume SMA"]
    if preset == "Advanced Ichimoku":
        return ["Ichimoku", "Supertrend", "ADX", "Volume SMA", "ATR"]
    if preset == "Pivots+CCI":
        return ["Pivot Points", "CCI", "Williams %R", "RSI", "Volume SMA"]
    return []


def resolve_enabled_indicators(params: dict[str, Any]) -> set[str]:
    raw_enabled = params.get("enabled", [])
    if raw_enabled:
        return {str(item) for item in raw_enabled}

    preset = str(params.get("preset", "Custom"))
    if preset not in VWAP_PRESETS:
        raise ValueError(f"Unsupported preset: {preset}")
    return set(apply_preset(preset))


def _fmt(value: object) -> float:
    if isinstance(value, bool):
        return np.nan
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return np.nan
    return np.nan


def compute_indicator_snapshot(
    row: pd.Series,
    ind: dict[str, pd.Series],
    idx: int,
) -> dict[str, float]:
    return {
        "close": _fmt(row["close"]),
        "ema_fast": _fmt(ind["ema_fast"].iloc[idx]),
        "ema_slow": _fmt(ind["ema_slow"].iloc[idx]),
        "vwap": _fmt(ind["vwap"].iloc[idx]),
        "rsi": _fmt(ind["rsi"].iloc[idx]),
        "stoch_k": _fmt(ind["stoch_k"].iloc[idx]),
        "stoch_d": _fmt(ind["stoch_d"].iloc[idx]),
        "macd_hist": _fmt(ind["macd_hist"].iloc[idx]),
        "adx": _fmt(ind["adx"].iloc[idx]),
        "atr": _fmt(ind["atr"].iloc[idx]),
        "bb_low": _fmt(ind["bb_low"].iloc[idx]),
        "bb_mid": _fmt(ind["bb_mid"].iloc[idx]),
        "bb_high": _fmt(ind["bb_high"].iloc[idx]),
        "bb_width": _fmt(ind["bb_width"].iloc[idx]),
        "vol": _fmt(row["volume"]),
        "vol_sma": _fmt(ind["vol_sma"].iloc[idx]),
        "ichimoku_conversion": _fmt(ind["ichimoku_conversion"].iloc[idx]),
        "ichimoku_base": _fmt(ind["ichimoku_base"].iloc[idx]),
        "ichimoku_lead_a": _fmt(ind["ichimoku_lead_a"].iloc[idx]),
        "ichimoku_lead_b": _fmt(ind["ichimoku_lead_b"].iloc[idx]),
        "supertrend_upper": _fmt(ind["supertrend_upper"].iloc[idx]),
        "supertrend_lower": _fmt(ind["supertrend_lower"].iloc[idx]),
        "pivot_s1": _fmt(ind["pivot_s1"].iloc[idx]),
        "pivot_r1": _fmt(ind["pivot_r1"].iloc[idx]),
        "cci": _fmt(ind["cci"].iloc[idx]),
        "willr": _fmt(ind["willr"].iloc[idx]),
    }


def long_conditions(
    snap: dict[str, float],
    enabled: set[str],
    regime: str,
) -> tuple[bool, list[str]]:
    if regime == "Bear":
        return False, ["Regime=Bear blocks LONG"]
    reasons: list[str] = []
    if "EMA Slow (50)" in enabled:
        reasons.append("ok" if snap["close"] > snap["ema_slow"] else "FAIL EMA50")
    if "EMA Fast (21)" in enabled:
        reasons.append("ok" if snap["close"] > snap["ema_fast"] else "FAIL EMA21")
    if "VWAP" in enabled:
        reasons.append("ok" if snap["close"] > snap["vwap"] else "FAIL VWAP")
    if "MACD" in enabled:
        reasons.append("ok" if snap["macd_hist"] > 0 else "FAIL MACD")
    if "RSI" in enabled:
        if regime == "Flat":
            reasons.append("ok" if snap["rsi"] < 30 else "FAIL RSI")
        else:
            reasons.append("ok" if snap["rsi"] > 50 else "FAIL RSI")
    if "Stoch RSI" in enabled:
        reasons.append("ok" if snap["stoch_k"] > snap["stoch_d"] else "FAIL STOCH")
    if "Bollinger Bands" in enabled:
        reasons.append(
            "ok" if snap["close"] < snap["bb_low"] or snap["close"] > snap["bb_mid"] else "FAIL BB"
        )
    if "Volume SMA" in enabled:
        reasons.append("ok" if snap["vol"] > snap["vol_sma"] else "FAIL VOL")
    if "Ichimoku" in enabled:
        cloud_top = max(snap["ichimoku_lead_a"], snap["ichimoku_lead_b"])
        reasons.append("ok" if snap["close"] > cloud_top else "FAIL ICHIMOKU")
    if "Supertrend" in enabled:
        reasons.append("ok" if snap["close"] > snap["supertrend_upper"] else "FAIL SUPER")
    if "Pivot Points" in enabled:
        reasons.append("ok" if snap["close"] > snap["pivot_s1"] else "FAIL PIVOT")
    if "CCI" in enabled:
        reasons.append("ok" if snap["cci"] < -100 else "FAIL CCI")
    if "Williams %R" in enabled:
        reasons.append("ok" if snap["willr"] < -80 else "FAIL WILLR")
    positives = len([reason for reason in reasons if reason == "ok"])
    return positives >= 2, reasons


def short_conditions(
    snap: dict[str, float],
    enabled: set[str],
    regime: str,
) -> tuple[bool, list[str]]:
    if regime == "Bull":
        return False, ["Regime=Bull blocks SHORT"]
    reasons: list[str] = []
    if "EMA Slow (50)" in enabled:
        reasons.append("ok" if snap["close"] < snap["ema_slow"] else "FAIL EMA50")
    if "EMA Fast (21)" in enabled:
        reasons.append("ok" if snap["close"] < snap["ema_fast"] else "FAIL EMA21")
    if "VWAP" in enabled:
        reasons.append("ok" if snap["close"] < snap["vwap"] else "FAIL VWAP")
    if "MACD" in enabled:
        reasons.append("ok" if snap["macd_hist"] < 0 else "FAIL MACD")
    if "RSI" in enabled:
        if regime == "Flat":
            reasons.append("ok" if snap["rsi"] > 70 else "FAIL RSI")
        else:
            reasons.append("ok" if snap["rsi"] < 50 else "FAIL RSI")
    if "Stoch RSI" in enabled:
        reasons.append("ok" if snap["stoch_k"] < snap["stoch_d"] else "FAIL STOCH")
    if "Bollinger Bands" in enabled:
        reasons.append(
            "ok" if snap["close"] > snap["bb_high"] or snap["close"] < snap["bb_mid"] else "FAIL BB"
        )
    if "Volume SMA" in enabled:
        reasons.append("ok" if snap["vol"] > snap["vol_sma"] else "FAIL VOL")
    if "Ichimoku" in enabled:
        cloud_bottom = min(snap["ichimoku_lead_a"], snap["ichimoku_lead_b"])
        reasons.append("ok" if snap["close"] < cloud_bottom else "FAIL ICHIMOKU")
    if "Supertrend" in enabled:
        reasons.append("ok" if snap["close"] < snap["supertrend_lower"] else "FAIL SUPER")
    if "Pivot Points" in enabled:
        reasons.append("ok" if snap["close"] < snap["pivot_r1"] else "FAIL PIVOT")
    if "CCI" in enabled:
        reasons.append("ok" if snap["cci"] > 100 else "FAIL CCI")
    if "Williams %R" in enabled:
        reasons.append("ok" if snap["willr"] > -20 else "FAIL WILLR")
    positives = len([reason for reason in reasons if reason == "ok"])
    return positives >= 2, reasons


def simulate_trades(
    df: pd.DataFrame,
    ind: dict[str, pd.Series],
    enabled: set[str],
    regime: str,
    rr: float,
    atr_mult: float,
    cooldown_bars: int,
    risk_per_trade: float,
    max_positions: int,
    account_balance: float,
    max_position_pct: float,
    stop_mode: str,
    swing_lookback: int,
    swing_buffer_atr: float,
    ob_impulse_atr: float,
    ob_buffer_atr: float,
    ob_lookback: int,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    sizer = PositionSizer(
        account_balance=account_balance,
        risk_per_trade=risk_per_trade,
        max_open_positions=max_positions,
        max_position_pct=max_position_pct,
    )
    i = 0
    while i < len(df) - 2:
        snap = compute_indicator_snapshot(df.iloc[i], ind, i)
        if not np.isfinite(snap["atr"]) or snap["atr"] <= 0:
            i += 1
            continue

        long_ok, long_reasons = long_conditions(snap, enabled, regime)
        short_ok, short_reasons = short_conditions(snap, enabled, regime)
        if not long_ok and not short_ok:
            i += 1
            continue
        side = "LONG" if long_ok and (not short_ok or regime != "Bear") else "SHORT"
        reasons = long_reasons if side == "LONG" else short_reasons
        entry = snap["close"]
        atr = snap["atr"]
        sl, sl_explain = compute_stop_loss(
            df=df,
            indicators=ind,
            idx=i,
            side=side,
            entry=entry,
            atr_mult=atr_mult,
            stop_mode=stop_mode,
            swing_lookback=swing_lookback,
            swing_buffer_atr=swing_buffer_atr,
            ob_impulse_atr=ob_impulse_atr,
            ob_buffer_atr=ob_buffer_atr,
            ob_lookback=ob_lookback,
        )
        tp = entry + (entry - sl) * rr if side == "LONG" else entry - (sl - entry) * rr
        sizing = sizer.calculate_position_size(entry, sl)
        if not bool(sizing["allowed"]):
            i += 1
            continue

        exit_i = len(df) - 1
        exit_price = float(df["close"].iloc[-1])
        exit_reason = "OPEN"
        for j in range(i + 1, len(df)):
            hi = float(df["high"].iloc[j])
            lo = float(df["low"].iloc[j])
            if side == "LONG":
                if lo <= sl:
                    exit_i, exit_price, exit_reason = j, sl, "STOP"
                    break
                if hi >= tp:
                    exit_i, exit_price, exit_reason = j, tp, "TAKE"
                    break
            else:
                if hi >= sl:
                    exit_i, exit_price, exit_reason = j, sl, "STOP"
                    break
                if lo <= tp:
                    exit_i, exit_price, exit_reason = j, tp, "TAKE"
                    break
        risk = abs(entry - sl)
        pnl = (exit_price - entry) if side == "LONG" else (entry - exit_price)
        qty = float(sizing["quantity"])
        trades.append(
            {
                "side": side,
                "entry_i": i,
                "exit_i": exit_i,
                "entry_time": str(df.index[i]),
                "exit_time": str(df.index[exit_i]),
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "exit": exit_price,
                "exit_reason": exit_reason,
                "rr_target": rr,
                "atr": atr,
                "atr_mult": atr_mult,
                "stop_mode": stop_mode,
                "sl_explain": sl_explain,
                "r_real": pnl / risk if risk > 0 else np.nan,
                "reasons": reasons,
                "snapshot": snap,
                "position_size": qty,
                "position_value": float(sizing["position_value"]),
                "risk_usdt": float(sizing["risk_usdt"]),
                "pnl_usdt": pnl * qty,
                "pnl_pct": (pnl / entry * 100) if entry > 0 else 0.0,
            }
        )
        i = exit_i + max(1, cooldown_bars)
    return trades


def run_vwap_backtest(
    df: pd.DataFrame,
    indicators: dict[str, pd.Series],
    params: dict[str, Any],
) -> dict[str, Any]:
    account_balance = float(params.get("account_balance", 1000.0))
    enabled = resolve_enabled_indicators(params)
    trades = simulate_trades(
        df=df,
        ind=indicators,
        enabled=enabled,
        regime=str(params.get("regime", "Flat")),
        rr=float(params.get("rr", 2.0)),
        atr_mult=float(params.get("atr_mult", 1.5)),
        cooldown_bars=int(params.get("cooldown_bars", 5)),
        risk_per_trade=float(params.get("risk_per_trade", 1.0)),
        max_positions=int(params.get("max_positions", 5)),
        account_balance=account_balance,
        max_position_pct=float(params.get("max_position_pct", 100.0)),
        stop_mode=str(params.get("stop_mode", "ATR")),
        swing_lookback=int(params.get("swing_lookback", 20)),
        swing_buffer_atr=float(params.get("swing_buffer_atr", 0.3)),
        ob_impulse_atr=float(params.get("ob_impulse_atr", 1.5)),
        ob_buffer_atr=float(params.get("ob_buffer_atr", 0.15)),
        ob_lookback=int(params.get("ob_lookback", 120)),
    )
    trades = annotate_trade_confirmations(trades)
    summary, equity_curve = add_capital_metrics(
        summary=calculate_performance_metrics(trades),
        trades=trades,
        initial_balance=account_balance,
    )
    chart_points = {
        "ohlcv": df.reset_index().to_dict(orient="records"),
        "ema_fast": indicators["ema_fast"].tolist(),
        "ema_slow": indicators["ema_slow"].tolist(),
        "vwap": indicators["vwap"].tolist(),
        "equity_curve": equity_curve,
    }
    return {
        "summary": summary,
        "trades": trades,
        "chart_points": chart_points,
        "explanations": [
            {
                "reasons": trade.get("reasons", []),
                "stop_mode": trade.get("stop_mode"),
                "sl_explain": trade.get("sl_explain", {}),
            }
            for trade in trades[:50]
        ],
    }
