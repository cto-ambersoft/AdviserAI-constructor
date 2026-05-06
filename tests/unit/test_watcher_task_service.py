"""Unit tests for watcher tick execution and scheduling helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.position.context import PositionContext, PositionSide, WatcherConfig  # noqa: E402
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.watchers.indicator_watcher import WatcherEvent  # noqa: E402
from app.services.watchers.scheduling import timeframe_to_cron  # noqa: E402
from app.services.watchers.service import run_position_watcher_tick  # noqa: E402


def _build_ohlcv(closes: list[float]) -> list[list[object]]:
    rows: list[list[object]] = []
    for index, close in enumerate(closes):
        rows.append(
            [
                1_700_000_000_000 + (index * 60_000),
                close - 1.0,
                close + 1.0,
                close - 1.5,
                close,
                1000.0,
            ]
        )
    return rows


def _rsi_closes_high() -> list[float]:
    base = [100.0 + (i * 0.5) for i in range(80)]
    close = base[-1]
    for step in ([1.2] * 17) + ([-1.0] * 3):
        close += step
        base.append(close)
    return base


def _rsi_trigger_position() -> PositionContext:
    return PositionContext(
        position_id="101",
        user_id="501",
        account_id="42",
        exchange="binance",
        symbol="BTC/USDT:USDT",
        state=PositionState.OPEN,
        side=PositionSide.LONG,
        active_watchers=[
            WatcherConfig(
                indicator="RSI",
                params={"period": 14, "timeframe": "15m"},
                condition="> 75",
                action="tighten_sl",
                action_params={"sl_offset_atr": 1.5},
                is_active=True,
            )
        ],
    )


@pytest.mark.asyncio
async def test_run_position_watcher_tick_fetches_klines_and_publishes_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = _rsi_trigger_position()
    publish = AsyncMock()
    fetch_ohlcv = AsyncMock(
        return_value=(
            pd.DataFrame(
                _build_ohlcv(_rsi_closes_high()),
                columns=["time", "open", "high", "low", "close", "volume"],
            )
            .assign(time=lambda df: pd.to_datetime(df["time"], unit="ms", utc=True))
            .set_index("time")
        )
    )

    monkeypatch.setattr(
        "app.services.watchers.service.load_position_context",
        AsyncMock(return_value=position),
    )
    monkeypatch.setattr(
        "app.services.watchers.service._WATCHER_MARKET_DATA.fetch_ohlcv",
        fetch_ohlcv,
    )
    monkeypatch.setattr(
        "app.services.watchers.event_bus.publish_watcher_event",
        publish,
    )

    result = await run_position_watcher_tick("101")

    assert result == {"position_id": "101", "status": "processed", "events": 1}
    fetch_ohlcv.assert_awaited_once_with(
        exchange_name="binance",
        symbol="BTC/USDT:USDT",
        timeframe="15m",
        bars=100,
        market_type="futures",
    )
    publish.assert_awaited_once()
    event = publish.await_args.args[0]
    assert isinstance(event, WatcherEvent)
    assert event.position_id == "101"
    assert event.indicator == "RSI"
    assert event.market_price is not None


@pytest.mark.asyncio
async def test_run_position_watcher_tick_skips_non_active_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = PositionContext(position_id="101", state=PositionState.CLOSED)
    fetch_ohlcv = AsyncMock()
    publish = AsyncMock()

    monkeypatch.setattr(
        "app.services.watchers.service.load_position_context",
        AsyncMock(return_value=position),
    )
    monkeypatch.setattr(
        "app.services.watchers.service._WATCHER_MARKET_DATA.fetch_ohlcv",
        fetch_ohlcv,
    )
    monkeypatch.setattr(
        "app.services.watchers.event_bus.publish_watcher_event",
        publish,
    )

    result = await run_position_watcher_tick("101")

    assert result == {"position_id": "101", "status": "inactive_state", "events": 0}
    fetch_ohlcv.assert_not_called()
    publish.assert_not_called()


def test_timeframe_to_cron_uses_fastest_supported_interval() -> None:
    assert timeframe_to_cron("15m") == "*/15 * * * *"
    assert timeframe_to_cron("1h") == "0 * * * *"
