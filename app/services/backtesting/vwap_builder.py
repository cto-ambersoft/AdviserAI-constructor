from typing import Any

import numpy as np
import pandas as pd

from app.services.backtesting.ai_forecast import (
    AiForecastOverlay,
    normalize_ai_regime,
    prepare_ai_forecast_frame,
)
from app.services.backtesting.ai_forecast import (
    resolve_ai_forecast_overlay_per_bar as resolve_common_ai_forecast_overlay_per_bar,
)
from app.services.backtesting.ai_forecast import (
    resolve_ai_regimes_per_bar as resolve_common_ai_regimes_per_bar,
)
from app.services.backtesting.common import (
    PositionSizer,
    add_capital_metrics,
    add_client_summary_fields,
    annotate_trade_confirmations,
    build_r_chart_points,
    calculate_performance_metrics,
)
from app.services.backtesting.cost_model import apply_cost_model, cost_model_from_params
from app.services.backtesting.stop_logic import compute_stop_loss

VWAP_PRESETS = {
    "Custom",
    "Trend",
    "Range",
    "Breakdown",
    "Advanced Ichimoku",
    "Pivots+CCI",
}


def _normalize_regime(value: object) -> str:
    return normalize_ai_regime(value)


def _prepare_ai_forecast_frame(ai_rows: list[dict[str, Any]]) -> pd.DataFrame:
    return prepare_ai_forecast_frame(ai_rows)


def _safe_float(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return np.nan
    return numeric if np.isfinite(numeric) else np.nan


def _resolve_threshold(value: object, default: float = 52.0) -> float:
    parsed = _safe_float(value)
    if not np.isfinite(parsed):
        return default
    return parsed


def resolve_ai_regimes_per_bar(
    market_index: pd.Index,
    ai_rows: list[dict[str, Any]],
    fallback_regime: str,
    bull_threshold: float,
    bear_threshold: float,
) -> list[str]:
    return resolve_common_ai_regimes_per_bar(
        market_index=market_index,
        ai_rows=ai_rows,
        fallback_regime=fallback_regime,
        bull_threshold=bull_threshold,
        bear_threshold=bear_threshold,
    )


def resolve_ai_forecast_overlay_per_bar(
    market_index: pd.Index,
    ai_rows: list[dict[str, Any]],
    fallback_regime: str,
    bull_threshold: float,
    bear_threshold: float,
) -> AiForecastOverlay:
    return resolve_common_ai_forecast_overlay_per_bar(
        market_index=market_index,
        ai_rows=ai_rows,
        fallback_regime=fallback_regime,
        bull_threshold=bull_threshold,
        bear_threshold=bear_threshold,
    )


def _build_ai_overlay_points(
    market_index: pd.Index,
    overlay: AiForecastOverlay,
) -> list[dict[str, Any]]:
    times = pd.to_datetime(market_index, utc=True)
    return [
        {
            "time": time.isoformat(),
            "regime": overlay.regimes[idx],
            "active": overlay.active[idx],
            "applied": overlay.applied[idx],
            "signal_time_utc": overlay.signal_times[idx],
            "horizon_end_utc": overlay.horizon_end_times[idx],
        }
        for idx, time in enumerate(times)
    ]


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
    ai_overlay: AiForecastOverlay | None = None,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    sizer = PositionSizer(
        account_balance=account_balance,
        risk_per_trade=risk_per_trade,
        max_open_positions=max_positions,
        max_position_pct=max_position_pct,
    )
    high_arr = df["high"].to_numpy(dtype=float, copy=False)
    low_arr = df["low"].to_numpy(dtype=float, copy=False)
    close_arr = df["close"].to_numpy(dtype=float, copy=False)
    volume_arr = df["volume"].to_numpy(dtype=float, copy=False)
    index_values = df.index.to_numpy(copy=False)

    indicator_arrays = {
        key: series.to_numpy(dtype=float, copy=False)
        for key, series in ind.items()
    }

    i = 0
    while i < len(df) - 2:
        snap = {
            "close": float(close_arr[i]),
            "ema_fast": float(indicator_arrays["ema_fast"][i]),
            "ema_slow": float(indicator_arrays["ema_slow"][i]),
            "vwap": float(indicator_arrays["vwap"][i]),
            "rsi": float(indicator_arrays["rsi"][i]),
            "stoch_k": float(indicator_arrays["stoch_k"][i]),
            "stoch_d": float(indicator_arrays["stoch_d"][i]),
            "macd_hist": float(indicator_arrays["macd_hist"][i]),
            "adx": float(indicator_arrays["adx"][i]),
            "atr": float(indicator_arrays["atr"][i]),
            "bb_low": float(indicator_arrays["bb_low"][i]),
            "bb_mid": float(indicator_arrays["bb_mid"][i]),
            "bb_high": float(indicator_arrays["bb_high"][i]),
            "bb_width": float(indicator_arrays["bb_width"][i]),
            "vol": float(volume_arr[i]),
            "vol_sma": float(indicator_arrays["vol_sma"][i]),
            "ichimoku_conversion": float(indicator_arrays["ichimoku_conversion"][i]),
            "ichimoku_base": float(indicator_arrays["ichimoku_base"][i]),
            "ichimoku_lead_a": float(indicator_arrays["ichimoku_lead_a"][i]),
            "ichimoku_lead_b": float(indicator_arrays["ichimoku_lead_b"][i]),
            "supertrend_upper": float(indicator_arrays["supertrend_upper"][i]),
            "supertrend_lower": float(indicator_arrays["supertrend_lower"][i]),
            "pivot_s1": float(indicator_arrays["pivot_s1"][i]),
            "pivot_r1": float(indicator_arrays["pivot_r1"][i]),
            "cci": float(indicator_arrays["cci"][i]),
            "willr": float(indicator_arrays["willr"][i]),
        }
        if not np.isfinite(snap["atr"]) or snap["atr"] <= 0:
            i += 1
            continue

        active_regime = ai_overlay.regimes[i] if ai_overlay is not None else regime
        ai_applied = bool(ai_overlay and i < len(ai_overlay.applied) and ai_overlay.applied[i])
        long_ok, long_reasons = long_conditions(snap, enabled, active_regime)
        short_ok, short_reasons = short_conditions(snap, enabled, active_regime)
        if not long_ok and not short_ok:
            i += 1
            continue
        side = "LONG" if long_ok and (not short_ok or active_regime != "Bear") else "SHORT"
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
        exit_price = float(close_arr[-1])
        exit_reason = "OPEN"
        for j in range(i + 1, len(df)):
            hi = float(high_arr[j])
            lo = float(low_arr[j])
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
        trade = {
            "side": side,
            "entry_i": i,
            "exit_i": exit_i,
            "entry_time": str(index_values[i]),
            "exit_time": str(index_values[exit_i]),
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "exit": exit_price,
            "exit_reason": exit_reason,
            "rr_target": rr,
            "atr": atr,
            "atr_mult": atr_mult,
            "regime": active_regime,
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
        if ai_overlay is not None and ai_applied:
            trade.update(
                {
                    "ai_forecast_applied": True,
                    "ai_base_regime": regime,
                    "ai_regime": active_regime,
                    "ai_regime_changed": active_regime != regime,
                    "ai_signal_time_utc": ai_overlay.signal_times[i],
                    "ai_horizon_end_utc": ai_overlay.horizon_end_times[i],
                }
            )
        trades.append(trade)
        i = exit_i + max(1, cooldown_bars)
    return trades


def run_vwap_backtest(
    df: pd.DataFrame,
    indicators: dict[str, pd.Series],
    params: dict[str, Any],
) -> dict[str, Any]:
    account_balance = float(params.get("account_balance", 1000.0))
    enabled = resolve_enabled_indicators(params)
    base_regime = _normalize_regime(params.get("regime", "Flat"))
    run_with_ai = bool(params.get("run_with_ai", False))
    ai_overlay: AiForecastOverlay | None = None
    if run_with_ai:
        precomputed = params.get("precomputed_ai_overlay")
        if isinstance(precomputed, AiForecastOverlay):
            ai_overlay = precomputed
        else:
            ai_rows = params.get("ai_forecast_rows")
            if not isinstance(ai_rows, list):
                raise ValueError("ai_forecast_rows are required for AI-enabled backtest run.")
            bull_threshold = _resolve_threshold(params.get("ai_bull_confidence_threshold"))
            bear_threshold = _resolve_threshold(params.get("ai_bear_confidence_threshold"))
            ai_overlay = resolve_ai_forecast_overlay_per_bar(
                market_index=df.index,
                ai_rows=ai_rows,
                fallback_regime=base_regime,
                bull_threshold=bull_threshold,
                bear_threshold=bear_threshold,
            )
    trades = simulate_trades(
        df=df,
        ind=indicators,
        enabled=enabled,
        regime=base_regime,
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
        ai_overlay=ai_overlay,
    )
    trades = annotate_trade_confirmations(trades)
    # Finding 7.4: net trading costs off P&L before metrics (no-op when costs are 0).
    trades = apply_cost_model(trades, cost_model_from_params(params))
    summary, equity_curve = add_capital_metrics(
        summary=calculate_performance_metrics(trades),
        trades=trades,
        initial_balance=account_balance,
        period_start=df.index[0] if len(df.index) else None,
        period_end=df.index[-1] if len(df.index) else None,
    )
    ai_explanation: dict[str, Any] | None = None
    if run_with_ai and ai_overlay is not None:
        regime_counts = {
            regime: int(ai_overlay.regimes.count(regime))
            for regime in ("Bull", "Flat", "Bear")
        }
        active_bars = int(sum(1 for active in ai_overlay.active if active))
        applied_bars = int(sum(1 for applied in ai_overlay.applied if applied))
        changed_bars = int(
            sum(
                1
                for regime, applied in zip(ai_overlay.regimes, ai_overlay.applied, strict=False)
                if applied and regime != base_regime
            )
        )
        summary.update(
            {
                "ai_forecast_applied": applied_bars > 0,
                "ai_base_regime": base_regime,
                "ai_regime_counts": regime_counts,
                "ai_forecast_active_bars": active_bars,
                "ai_forecast_applied_bars": applied_bars,
                "ai_regime_changed_bars": changed_bars,
            }
        )
        summary = add_client_summary_fields(summary)
        ai_explanation = {
            "type": "ai_forecast",
            "base_regime": base_regime,
            "regime_counts": regime_counts,
            "active_bars": active_bars,
            "applied_bars": applied_bars,
            "changed_bars": changed_bars,
            "thresholds": {
                "bull": _resolve_threshold(params.get("ai_bull_confidence_threshold")),
                "bear": _resolve_threshold(params.get("ai_bear_confidence_threshold")),
            },
        }
    r_chart_points = build_r_chart_points(trades)
    include_series = bool(params.get("include_series", True))
    if include_series:
        chart_points = {
            "ohlcv": df.reset_index().to_dict(orient="records"),
            "ema_fast": indicators["ema_fast"].tolist(),
            "ema_slow": indicators["ema_slow"].tolist(),
            "vwap": indicators["vwap"].tolist(),
            "equity_curve": equity_curve,
            **r_chart_points,
        }
        if ai_overlay is not None:
            chart_points["ai_forecast_overlay"] = _build_ai_overlay_points(df.index, ai_overlay)
    else:
        chart_points = {}
    explanations: list[Any] = [
        {
            "reasons": trade.get("reasons", []),
            "stop_mode": trade.get("stop_mode"),
            "sl_explain": trade.get("sl_explain", {}),
        }
        for trade in trades[:50]
    ]
    if ai_explanation is not None:
        explanations.append(ai_explanation)

    return {
        "summary": summary,
        "trades": trades,
        "chart_points": chart_points,
        "explanations": explanations,
    }
