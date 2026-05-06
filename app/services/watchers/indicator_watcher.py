"""Indicator computation + normalization + condition evaluation."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pandas_ta as ta

from app.services.position.context import PositionContext
from app.services.watchers import rule_engine

logger = logging.getLogger(__name__)

ComputeFn = Callable[[pd.DataFrame, dict[str, Any]], Any]
NormalizeFn = Callable[[Any, dict[str, Any]], Any]
MinBarsFn = Callable[[dict[str, Any]], int]


@dataclass(frozen=True)
class WatcherEvent:
    position_id: str
    indicator: str
    condition: str
    current_value: Any
    action: str
    action_params: dict[str, Any]
    timestamp: str
    market_price: float | None = None


@dataclass(frozen=True)
class IndicatorSpec:
    compute: ComputeFn
    normalize: NormalizeFn
    min_bars: MinBarsFn


def _get_positive_int(
    params: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: int,
) -> int:
    for key in keys:
        raw = params.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return default


def _as_finite_float(value: Any, *, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Cannot cast {label} to float.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite float in {label}.")
    return parsed


def _normalize_scalar_series(raw_result: Any, _params: dict[str, Any]) -> float:
    if not isinstance(raw_result, pd.Series):
        raise TypeError("Expected pandas Series for scalar indicator normalization.")
    if raw_result.empty:
        raise ValueError("Cannot normalize empty series.")
    return _as_finite_float(raw_result.iloc[-1], label="series[-1]")


def _normalize_macd(raw_result: Any, params: dict[str, Any]) -> dict[str, float]:
    if not isinstance(raw_result, pd.DataFrame):
        raise TypeError("Expected pandas DataFrame for MACD normalization.")
    if len(raw_result) < 2:
        raise ValueError("MACD normalization requires at least two bars.")

    fast = _get_positive_int(params, ("fast",), default=12)
    slow = _get_positive_int(params, ("slow",), default=26)
    signal = _get_positive_int(params, ("signal",), default=9)
    line_col = f"MACD_{fast}_{slow}_{signal}"
    signal_col = f"MACDs_{fast}_{slow}_{signal}"
    if line_col not in raw_result.columns or signal_col not in raw_result.columns:
        raise KeyError(
            f"MACD output columns are missing. Expected '{line_col}' and '{signal_col}'."
        )

    current_row = raw_result.iloc[-1]
    previous_row = raw_result.iloc[-2]
    return {
        "line": _as_finite_float(current_row[line_col], label="macd.line"),
        "signal": _as_finite_float(current_row[signal_col], label="macd.signal"),
        "prev_line": _as_finite_float(previous_row[line_col], label="macd.prev_line"),
        "prev_signal": _as_finite_float(previous_row[signal_col], label="macd.prev_signal"),
    }


def _normalize_ema_cross(raw_result: Any, _params: dict[str, Any]) -> dict[str, float]:
    if not isinstance(raw_result, tuple) or len(raw_result) != 2:
        raise TypeError("EMA_CROSS normalization expects (fast_ema, slow_ema) tuple.")

    fast_ema, slow_ema = raw_result
    if not isinstance(fast_ema, pd.Series) or not isinstance(slow_ema, pd.Series):
        raise TypeError("EMA_CROSS values must be pandas Series.")
    if len(fast_ema) < 2 or len(slow_ema) < 2:
        raise ValueError("EMA_CROSS normalization requires at least two bars.")

    return {
        "line": _as_finite_float(fast_ema.iloc[-1], label="ema_cross.line"),
        "signal": _as_finite_float(slow_ema.iloc[-1], label="ema_cross.signal"),
        "prev_line": _as_finite_float(fast_ema.iloc[-2], label="ema_cross.prev_line"),
        "prev_signal": _as_finite_float(slow_ema.iloc[-2], label="ema_cross.prev_signal"),
    }


def _compute_rsi(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    period = _get_positive_int(params, ("period",), default=14)
    return ta.rsi(df["close"], length=period)


def _compute_macd(df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    fast = _get_positive_int(params, ("fast",), default=12)
    slow = _get_positive_int(params, ("slow",), default=26)
    signal = _get_positive_int(params, ("signal",), default=9)
    return ta.macd(df["close"], fast=fast, slow=slow, signal=signal)


def _compute_atr(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    period = _get_positive_int(params, ("period",), default=14)
    return ta.atr(df["high"], df["low"], df["close"], length=period)


def _compute_ema(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    period = _get_positive_int(params, ("period",), default=21)
    return ta.ema(df["close"], length=period)


def _compute_sma(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    period = _get_positive_int(params, ("period",), default=50)
    return ta.sma(df["close"], length=period)


def _compute_ema_cross(df: pd.DataFrame, params: dict[str, Any]) -> tuple[pd.Series, pd.Series]:
    fast_period = _get_positive_int(params, ("fast_period", "fast"), default=21)
    slow_period = _get_positive_int(params, ("slow_period", "slow"), default=50)
    return (
        ta.ema(df["close"], length=fast_period),
        ta.ema(df["close"], length=slow_period),
    )


def _rsi_min_bars(params: dict[str, Any]) -> int:
    return _get_positive_int(params, ("period",), default=14) + 10


def _macd_min_bars(params: dict[str, Any]) -> int:
    slow = _get_positive_int(params, ("slow",), default=26)
    signal = _get_positive_int(params, ("signal",), default=9)
    return slow + signal + 10


def _atr_min_bars(params: dict[str, Any]) -> int:
    return _get_positive_int(params, ("period",), default=14) + 10


def _ema_min_bars(params: dict[str, Any]) -> int:
    return _get_positive_int(params, ("period",), default=21) + 10


def _sma_min_bars(params: dict[str, Any]) -> int:
    return _get_positive_int(params, ("period",), default=50) + 10


def _ema_cross_min_bars(params: dict[str, Any]) -> int:
    slow_period = _get_positive_int(params, ("slow_period", "slow"), default=50)
    return slow_period + 10


class IndicatorWatcher:
    """Compute configured indicators and evaluate watcher rules."""

    INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {
        "RSI": IndicatorSpec(
            compute=_compute_rsi,
            normalize=_normalize_scalar_series,
            min_bars=_rsi_min_bars,
        ),
        "MACD": IndicatorSpec(
            compute=_compute_macd,
            normalize=_normalize_macd,
            min_bars=_macd_min_bars,
        ),
        "ATR": IndicatorSpec(
            compute=_compute_atr,
            normalize=_normalize_scalar_series,
            min_bars=_atr_min_bars,
        ),
        "EMA": IndicatorSpec(
            compute=_compute_ema,
            normalize=_normalize_scalar_series,
            min_bars=_ema_min_bars,
        ),
        "SMA": IndicatorSpec(
            compute=_compute_sma,
            normalize=_normalize_scalar_series,
            min_bars=_sma_min_bars,
        ),
        "EMA_CROSS": IndicatorSpec(
            compute=_compute_ema_cross,
            normalize=_normalize_ema_cross,
            min_bars=_ema_cross_min_bars,
        ),
    }

    def __init__(self, position: PositionContext) -> None:
        self.position = position

    def tick(self, kline_buffers: dict[str, pd.DataFrame]) -> list[WatcherEvent]:
        """Evaluate active watchers against timeframe kline buffers."""
        events: list[WatcherEvent] = []

        for watcher in self.position.active_watchers:
            if not watcher.is_active:
                continue

            spec = self.INDICATOR_REGISTRY.get(watcher.indicator)
            if spec is None:
                logger.warning(
                    "Unsupported watcher indicator '%s' for position %s. Skipping.",
                    watcher.indicator,
                    self.position.position_id,
                )
                continue

            timeframe_raw = watcher.params.get("timeframe", "15m")
            timeframe = str(timeframe_raw)
            df = kline_buffers.get(timeframe)
            if df is None or df.empty:
                continue

            min_bars = spec.min_bars(watcher.params)
            if len(df) < min_bars:
                continue

            try:
                raw_result = spec.compute(df, watcher.params)
                normalized_value = spec.normalize(raw_result, watcher.params)
            except Exception:
                logger.warning(
                    "Failed to compute watcher indicator '%s' for position %s. Skipping.",
                    watcher.indicator,
                    self.position.position_id,
                    exc_info=True,
                )
                continue

            if rule_engine.evaluate_condition(watcher.condition, normalized_value):
                market_price = _as_finite_float(df["close"].iloc[-1], label="close[-1]")
                events.append(
                    WatcherEvent(
                        position_id=self.position.position_id,
                        indicator=watcher.indicator,
                        condition=watcher.condition,
                        current_value=normalized_value,
                        action=watcher.action,
                        action_params=dict(watcher.action_params),
                        timestamp=datetime.now(UTC).isoformat(),
                        market_price=market_price,
                    )
                )

        return events
