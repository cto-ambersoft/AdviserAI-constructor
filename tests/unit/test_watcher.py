"""Unit tests for indicator watcher computation + rule evaluation."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.position.context import PositionContext, WatcherConfig  # noqa: E402
from app.services.watchers.indicator_watcher import IndicatorWatcher  # noqa: E402


def _build_ohlc_df(closes: list[float], *, range_width: float = 2.0) -> pd.DataFrame:
    close_series = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close_series.shift(1).fillna(close_series.iloc[0]),
            "high": close_series + (range_width / 2.0),
            "low": close_series - (range_width / 2.0),
            "close": close_series,
            "volume": 1000.0,
        }
    )


def _rsi_closes_high() -> list[float]:
    base = [100.0 + (i * 0.5) for i in range(80)]
    close = base[-1]
    for step in ([1.2] * 17) + ([-1.0] * 3):
        close += step
        base.append(close)
    return base


def _rsi_closes_mid() -> list[float]:
    base = [100.0 + (i * 0.5) for i in range(80)]
    close = base[-1]
    for step in ([1.0] * 15) + ([-0.8] * 5):
        close += step
        base.append(close)
    return base


def _macd_cross_below_closes() -> list[float]:
    return [float(100 + i) for i in range(100)] + [199.0, 201.0, 203.0, 195.0]


def _macd_already_below_closes() -> list[float]:
    return _macd_cross_below_closes() + [194.0]


def _ema_cross_above_closes() -> list[float]:
    return [float(300 - i) for i in range(120)] + [177.0, 177.0, 188.0, 189.0]


def _atr_closes() -> list[float]:
    return [10000.0 + (i * 5.0) for i in range(80)]


def _watcher(
    *,
    indicator: str,
    condition: str,
    timeframe: str = "15m",
    params: dict[str, Any] | None = None,
    action: str = "tighten_sl",
    action_params: dict[str, Any] | None = None,
    is_active: bool = True,
) -> WatcherConfig:
    merged_params: dict[str, Any] = {"timeframe": timeframe}
    if params:
        merged_params.update(params)
    return WatcherConfig(
        indicator=indicator,
        params=merged_params,
        condition=condition,
        action=action,
        action_params=action_params or {},
        is_active=is_active,
    )


def _position(*watchers: WatcherConfig) -> PositionContext:
    return PositionContext(position_id="pos-123", active_watchers=list(watchers))


def test_tick_rsi_scalar_triggers_event_when_above_threshold() -> None:
    watcher = _watcher(indicator="RSI", params={"period": 14}, condition="> 75")
    position = _position(watcher)
    watcher_engine = IndicatorWatcher(position)
    kline = {"15m": _build_ohlc_df(_rsi_closes_high())}

    events = watcher_engine.tick(kline)

    assert len(events) == 1
    event = events[0]
    assert event.position_id == "pos-123"
    assert event.indicator == "RSI"
    assert event.condition == "> 75"
    assert event.action == "tighten_sl"
    assert event.current_value == pytest.approx(80.088, abs=1.0)
    datetime.fromisoformat(event.timestamp)


def test_tick_rsi_does_not_trigger_when_threshold_not_met() -> None:
    watcher = _watcher(indicator="RSI", params={"period": 14}, condition="> 75")
    watcher_engine = IndicatorWatcher(_position(watcher))
    kline = {"15m": _build_ohlc_df(_rsi_closes_mid())}

    events = watcher_engine.tick(kline)

    assert events == []


def test_tick_rsi_uses_user_threshold_not_hardcoded() -> None:
    watcher = _watcher(indicator="RSI", params={"period": 14}, condition="> 60")
    watcher_engine = IndicatorWatcher(_position(watcher))
    kline = {"15m": _build_ohlc_df(_rsi_closes_mid())}

    events = watcher_engine.tick(kline)

    assert len(events) == 1
    assert events[0].current_value == pytest.approx(69.956, abs=1.0)


def test_tick_macd_cross_below_triggers_event() -> None:
    watcher = _watcher(
        indicator="MACD",
        condition="cross_below",
        params={"fast": 12, "slow": 26, "signal": 9},
    )
    watcher_engine = IndicatorWatcher(_position(watcher))
    kline = {"15m": _build_ohlc_df(_macd_cross_below_closes())}

    events = watcher_engine.tick(kline)

    assert len(events) == 1
    event = events[0]
    assert event.indicator == "MACD"
    assert isinstance(event.current_value, dict)
    assert event.current_value["line"] < event.current_value["signal"]
    assert event.current_value["prev_line"] >= event.current_value["prev_signal"]


def test_tick_macd_cross_below_not_triggered_when_already_below() -> None:
    watcher = _watcher(
        indicator="MACD",
        condition="cross_below",
        params={"fast": 12, "slow": 26, "signal": 9},
    )
    watcher_engine = IndicatorWatcher(_position(watcher))
    kline = {"15m": _build_ohlc_df(_macd_already_below_closes())}

    events = watcher_engine.tick(kline)

    assert events == []


def test_tick_ema_cross_above_triggers_event() -> None:
    watcher = _watcher(
        indicator="EMA_CROSS",
        condition="cross_above",
        params={"fast": 5, "slow": 10},
    )
    watcher_engine = IndicatorWatcher(_position(watcher))
    kline = {"15m": _build_ohlc_df(_ema_cross_above_closes())}

    events = watcher_engine.tick(kline)

    assert len(events) == 1
    event = events[0]
    assert event.indicator == "EMA_CROSS"
    assert event.current_value["line"] > event.current_value["signal"]
    assert event.current_value["prev_line"] <= event.current_value["prev_signal"]


def test_tick_atr_threshold_triggers_and_skips_correctly() -> None:
    watcher = _watcher(indicator="ATR", condition="> 1200", params={"period": 14})
    watcher_engine = IndicatorWatcher(_position(watcher))
    high_vol_kline = {"15m": _build_ohlc_df(_atr_closes(), range_width=1500.0)}
    low_vol_kline = {"15m": _build_ohlc_df(_atr_closes(), range_width=1000.0)}

    high_vol_events = watcher_engine.tick(high_vol_kline)
    low_vol_events = watcher_engine.tick(low_vol_kline)

    assert len(high_vol_events) == 1
    assert high_vol_events[0].current_value == pytest.approx(1500.0, abs=1.0)
    assert low_vol_events == []


def test_tick_multiple_watchers_trigger_two_events() -> None:
    rsi = _watcher(indicator="RSI", condition="> 60", params={"period": 14})
    macd = _watcher(
        indicator="MACD",
        condition="cross_below",
        params={"fast": 12, "slow": 26, "signal": 9},
        action="alert",
    )
    watcher_engine = IndicatorWatcher(_position(rsi, macd))
    kline = {"15m": _build_ohlc_df(_macd_cross_below_closes())}

    events = watcher_engine.tick(kline)

    assert len(events) == 2
    assert {event.indicator for event in events} == {"RSI", "MACD"}


def test_tick_skips_inactive_watcher() -> None:
    watcher = _watcher(indicator="RSI", condition="> 60", is_active=False)
    watcher_engine = IndicatorWatcher(_position(watcher))
    kline = {"15m": _build_ohlc_df(_rsi_closes_high())}

    events = watcher_engine.tick(kline)

    assert events == []


def test_tick_skips_when_insufficient_bars() -> None:
    watcher = _watcher(indicator="RSI", params={"period": 14}, condition="> 75")
    watcher_engine = IndicatorWatcher(_position(watcher))
    short_df = _build_ohlc_df(_rsi_closes_high()[:20])
    kline = {"15m": short_df}

    events = watcher_engine.tick(kline)

    assert events == []


def test_tick_skips_unknown_indicator_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    watcher = _watcher(indicator="RSI", condition="> 75")
    watcher.indicator = "BOLLINGER_FANCY"
    watcher_engine = IndicatorWatcher(_position(watcher))
    kline = {"15m": _build_ohlc_df(_rsi_closes_high())}

    with caplog.at_level(logging.WARNING, logger="app.services.watchers.indicator_watcher"):
        events = watcher_engine.tick(kline)

    assert events == []
    assert "Unsupported watcher indicator 'BOLLINGER_FANCY'" in caplog.text


def test_tick_skips_when_timeframe_missing_in_buffer() -> None:
    watcher = _watcher(indicator="RSI", params={"period": 14}, condition="> 75", timeframe="1h")
    watcher_engine = IndicatorWatcher(_position(watcher))
    kline = {"15m": _build_ohlc_df(_rsi_closes_high())}

    events = watcher_engine.tick(kline)

    assert events == []
