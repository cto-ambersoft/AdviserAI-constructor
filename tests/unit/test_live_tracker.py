"""Unit tests for the realtime SL adjustment tracker."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pandas_ta as ta  # noqa: E402

from app.services.position.context import PositionContext, PositionSide  # noqa: E402
from app.services.position.order_queue import OrderPriority, OrderTask  # noqa: E402
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.sl_tp import live_tracker as lt_module  # noqa: E402
from app.services.sl_tp.live_tracker import RealtimeSLAdjuster  # noqa: E402

SYMBOL = "BTC/USDT:USDT"


# ────────────────────────── helpers ───────────────────────────────────────


def _build_clock() -> tuple[list[float], Any]:
    """Return a list-backed monotonic clock that lets tests advance time deterministically."""
    state = [1_000.0]

    def now() -> float:
        return state[0]

    return state, now


def _build_long_position(
    *,
    position_id: str = "p-1",
    entry_price: float = 100_000.0,
    current_sl_price: float = 98_000.0,
    trailing: bool = True,
    breakeven: bool = False,
    volatility: bool = False,
    sl_order_id: str | None = "sl-order-1",
    state: PositionState = PositionState.OPEN,
    quantity: float = 0.5,
) -> PositionContext:
    return PositionContext(
        position_id=position_id,
        symbol=SYMBOL,
        state=state,
        side=PositionSide.LONG,
        entry_price=entry_price,
        current_quantity=quantity,
        original_quantity=quantity,
        current_sl_price=current_sl_price,
        sl_exchange_order_id=sl_order_id,
        trailing_enabled=trailing,
        trailing_callback_rate=1.0 if trailing else None,
        breakeven_enabled=breakeven,
        breakeven_trigger_rr=1.0,
        volatility_sl_enabled=volatility,
        volatility_atr_period=14,
        volatility_atr_multiplier=2.0,
        adjustment_priority=["trailing", "breakeven", "volatility"],
    )


def _kline(
    *,
    open_time: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    is_closed: bool = True,
    volume: float = 1.0,
) -> dict[str, Any]:
    return {
        "open_time": open_time,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "is_closed": is_closed,
    }


class _RecordingQueue:
    def __init__(self) -> None:
        self.tasks: list[OrderTask] = []

    async def enqueue(self, task: OrderTask) -> None:
        self.tasks.append(task)


def _build_adjuster(
    *,
    queue: _RecordingQueue | None = None,
    persist: AsyncMock | None = None,
    time_source: Any = None,
    throttle_seconds: float = 3.0,
    buffer_bars: int = 200,
    kill_switch_handler: Any = None,
) -> tuple[RealtimeSLAdjuster, _RecordingQueue, AsyncMock]:
    queue = queue if queue is not None else _RecordingQueue()
    persist = persist if persist is not None else AsyncMock()
    state, default_now = _build_clock()
    now = time_source if time_source is not None else default_now

    async def queue_resolver(_position: PositionContext) -> _RecordingQueue:
        return queue

    def coid_factory(position_id: str, kind: str) -> str:
        return f"{position_id}-{kind}-1"

    adjuster = RealtimeSLAdjuster(
        symbol=SYMBOL,
        queue_resolver=queue_resolver,
        client_order_id_factory=coid_factory,
        persist_handler=persist,
        buffer_bars=buffer_bars,
        throttle_seconds=throttle_seconds,
        time_source=now,
        kill_switch_handler=kill_switch_handler,
    )
    return adjuster, queue, persist


class _RecordingKillSwitch:
    """Async handler stand-in: records (position_id, signal) per kill-switch trip."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def __call__(self, position: PositionContext, signal: Any) -> None:
        self.calls.append((position.position_id, signal))


# ────────────────────────── needs_pipeline ────────────────────────────────


def test_needs_pipeline_true_when_any_dynamic_sl_enabled() -> None:
    base = PositionContext(symbol=SYMBOL)
    assert RealtimeSLAdjuster.needs_pipeline(base) is False

    for flag in ("trailing_enabled", "breakeven_enabled", "volatility_sl_enabled"):
        position = PositionContext(symbol=SYMBOL)
        setattr(position, flag, True)
        assert RealtimeSLAdjuster.needs_pipeline(position) is True


def test_needs_realtime_monitoring_includes_kill_switch() -> None:
    # T2.3b — the realtime tick must run for a kill-switch-only position too
    # (no SL pipeline), else on_tick never evaluates the spike detector.
    base = PositionContext(symbol=SYMBOL)
    assert RealtimeSLAdjuster.needs_realtime_monitoring(base) is False

    ks_only = PositionContext(symbol=SYMBOL)
    ks_only.kill_switch_enabled = True
    assert RealtimeSLAdjuster.needs_pipeline(ks_only) is False
    assert RealtimeSLAdjuster.needs_realtime_monitoring(ks_only) is True

    sl_only = PositionContext(symbol=SYMBOL)
    sl_only.trailing_enabled = True
    assert RealtimeSLAdjuster.needs_realtime_monitoring(sl_only) is True


# ────────────────────────── buffer ────────────────────────────────────────


def test_update_buffer_appends_closed_bars_in_order() -> None:
    adjuster, _, _ = _build_adjuster()

    bar1 = adjuster.update_buffer(_kline(open_time=1, open_=100, high=101, low=99, close=100))
    bar2 = adjuster.update_buffer(_kline(open_time=2, open_=100, high=102, low=99, close=101))

    assert bar1 is not None
    assert bar2 is not None
    assert [b["open_time"] for b in adjuster.buffer] == [1, 2]
    assert adjuster.buffer[-1]["close"] == 101


def test_update_buffer_replaces_in_progress_bar_with_same_open_time() -> None:
    adjuster, _, _ = _build_adjuster()

    adjuster.update_buffer(
        _kline(open_time=10, open_=100, high=100, low=100, close=100, is_closed=False)
    )
    adjuster.update_buffer(
        _kline(open_time=10, open_=100, high=105, low=99, close=104, is_closed=False)
    )
    adjuster.update_buffer(
        _kline(open_time=10, open_=100, high=110, low=98, close=109, is_closed=True)
    )

    assert len(adjuster.buffer) == 1
    assert adjuster.buffer[0]["close"] == 109
    assert adjuster.buffer[0]["high"] == 110
    assert adjuster.buffer[0]["is_closed"] is True


def test_update_buffer_caps_at_max_bars() -> None:
    adjuster, _, _ = _build_adjuster(buffer_bars=3)

    for index in range(5):
        adjuster.update_buffer(
            _kline(
                open_time=index,
                open_=100 + index,
                high=101 + index,
                low=99 + index,
                close=100 + index,
            )
        )

    assert len(adjuster.buffer) == 3
    assert [b["open_time"] for b in adjuster.buffer] == [2, 3, 4]


def test_update_buffer_returns_none_for_invalid_event() -> None:
    adjuster, _, _ = _build_adjuster()
    assert adjuster.update_buffer({"foo": "bar"}) is None
    assert adjuster.buffer == []


# ────────────────────────── ATR ───────────────────────────────────────────


def test_compute_atr_returns_none_for_insufficient_buffer() -> None:
    adjuster, _, _ = _build_adjuster()
    for index in range(10):
        adjuster.update_buffer(
            _kline(
                open_time=index,
                open_=100,
                high=101,
                low=99,
                close=100,
            )
        )
    # period 14 needs 15+ bars
    assert adjuster.compute_atr(14) is None


def test_compute_atr_returns_value_for_sufficient_buffer() -> None:
    adjuster, _, _ = _build_adjuster()
    for index in range(20):
        adjuster.update_buffer(
            _kline(
                open_time=index,
                open_=100.0 + index,
                high=102.0 + index,
                low=98.0 + index,
                close=100.0 + index,
            )
        )
    atr = adjuster.compute_atr(14)
    assert atr is not None
    assert atr > 0


# ────────────────────────── on_tick: trailing ─────────────────────────────


async def test_on_tick_trailing_enqueues_replace_sl_when_price_advances() -> None:
    adjuster, queue, persist = _build_adjuster()
    position = _build_long_position()

    adjusted = await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )

    assert adjusted == ["p-1"]
    assert len(queue.tasks) == 1
    task = queue.tasks[0]
    assert task.action == "replace_sl"
    assert task.priority == OrderPriority.SL_ADJUSTMENT
    assert task.params["existing_order_id"] == "sl-order-1"
    # New SL = highest * (1 - 1%) = 102000 * 0.99
    assert task.params["new_trigger_price"] == pytest.approx(100_980.0)
    assert position.current_sl_price == pytest.approx(100_980.0)
    assert position.trailing_highest_price == pytest.approx(102_000.0)
    persist.assert_awaited_once()


async def test_on_tick_skips_when_pipeline_disabled() -> None:
    adjuster, queue, _ = _build_adjuster()
    position = _build_long_position(trailing=False, breakeven=False, volatility=False)

    adjusted = await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )

    assert adjusted == []
    assert queue.tasks == []


async def test_on_tick_skips_when_state_not_open() -> None:
    adjuster, queue, _ = _build_adjuster()
    position = _build_long_position(state=PositionState.PENDING)

    adjusted = await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )

    assert adjusted == []
    assert queue.tasks == []


async def test_on_tick_skips_when_symbol_mismatch() -> None:
    adjuster, queue, _ = _build_adjuster()
    position = _build_long_position()
    position.symbol = "ETH/USDT:USDT"

    adjusted = await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )

    assert adjusted == []
    assert queue.tasks == []


async def test_on_tick_throttle_prevents_back_to_back_replacements() -> None:
    state, now = _build_clock()
    adjuster, queue, _ = _build_adjuster(time_source=now, throttle_seconds=3.0)
    position = _build_long_position()

    await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )
    state[0] += 0.5  # within throttle window
    await adjuster.on_tick(
        _kline(open_time=2, open_=102_000, high=103_000, low=101_500, close=103_000),
        [position],
    )

    assert len(queue.tasks) == 1


async def test_on_tick_throttle_releases_after_window() -> None:
    state, now = _build_clock()
    adjuster, queue, _ = _build_adjuster(time_source=now, throttle_seconds=3.0)
    position = _build_long_position()

    await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )
    state[0] += 5.0  # beyond throttle
    await adjuster.on_tick(
        _kline(open_time=2, open_=102_000, high=103_000, low=101_500, close=103_000),
        [position],
    )

    assert len(queue.tasks) == 2
    assert queue.tasks[1].params["new_trigger_price"] == pytest.approx(101_970.0)


async def test_on_tick_skips_when_no_sl_exchange_order_id() -> None:
    adjuster, queue, persist = _build_adjuster()
    position = _build_long_position(sl_order_id=None)

    adjusted = await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )

    assert adjusted == []
    assert queue.tasks == []
    persist.assert_not_awaited()
    # Even though we didn't dispatch, state was still updated to keep tracking consistent.
    assert position.trailing_highest_price == pytest.approx(102_000.0)


async def test_on_tick_breakeven_only_fires_once() -> None:
    adjuster, queue, _ = _build_adjuster(throttle_seconds=0.0)
    position = _build_long_position(trailing=False, breakeven=True)

    await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_500, low=100_500, close=102_500),
        [position],
    )
    await adjuster.on_tick(
        _kline(open_time=2, open_=102_500, high=103_000, low=101_500, close=103_000),
        [position],
    )

    assert len(queue.tasks) == 1
    assert queue.tasks[0].params["new_trigger_price"] == pytest.approx(100_000.0)
    assert position.breakeven_activated is True


async def test_on_tick_volatility_uses_buffer_atr() -> None:
    adjuster, queue, _ = _build_adjuster()
    # Pre-fill buffer with stable bars (ATR period 14 → need 15+).
    for index in range(20):
        adjuster.update_buffer(
            _kline(
                open_time=index,
                open_=100_000.0,
                high=100_500.0,
                low=99_500.0,
                close=100_000.0,
            )
        )

    position = _build_long_position(
        trailing=False,
        volatility=True,
        current_sl_price=80_000.0,  # very loose so volatility candidate is more protective
    )
    adjusted = await adjuster.on_tick(
        _kline(open_time=21, open_=100_000, high=100_500, low=99_500, close=100_000),
        [position],
    )
    assert adjusted == ["p-1"]
    assert len(queue.tasks) == 1
    # New SL computed by evaluator: entry - ATR*multiplier; should be > 80000 (more protective).
    assert queue.tasks[0].params["new_trigger_price"] > 80_000.0
    assert position.volatility_last_atr is not None
    assert position.volatility_last_atr > 0


async def test_on_tick_dispatches_only_for_matching_symbol() -> None:
    adjuster, queue, _ = _build_adjuster()
    p_match = _build_long_position(position_id="p-match")
    p_other = _build_long_position(position_id="p-other")
    p_other.symbol = "ETH/USDT:USDT"

    adjusted = await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [p_match, p_other],
    )

    assert adjusted == ["p-match"]
    assert len(queue.tasks) == 1


async def test_discard_position_clears_throttle_state() -> None:
    state, now = _build_clock()
    adjuster, queue, _ = _build_adjuster(time_source=now, throttle_seconds=3.0)
    position = _build_long_position()

    await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )
    assert len(queue.tasks) == 1

    adjuster.discard_position("p-1")
    state[0] += 0.5  # still inside the original throttle window
    # New position with same id but trailing reset — needs a fresh advance.
    fresh = _build_long_position()
    await adjuster.on_tick(
        _kline(open_time=2, open_=103_000, high=104_000, low=102_500, close=104_000),
        [fresh],
    )

    assert len(queue.tasks) == 2  # second dispatch went through after discard


async def test_on_tick_returns_no_ids_for_zero_or_negative_close() -> None:
    adjuster, queue, _ = _build_adjuster()
    position = _build_long_position()

    adjusted = await adjuster.on_tick(
        _kline(open_time=1, open_=0, high=0, low=0, close=0),
        [position],
    )

    assert adjusted == []
    assert queue.tasks == []


# ────────────────────────── on_tick: kill-switch (W9 T2.3) ─────────────────


def _arm_kill_switch(
    position: PositionContext,
    *,
    spike_mult: float | None = None,
    price_move_pct: float | None = None,
    cooldown: int | None = None,
) -> PositionContext:
    position.kill_switch_enabled = True
    position.kill_switch_atr_spike_mult = spike_mult
    position.kill_switch_price_move_pct = price_move_pct
    position.kill_switch_cooldown_seconds = cooldown
    return position


async def test_kill_switch_closes_on_price_move_spike() -> None:
    handler = _RecordingKillSwitch()
    adjuster, queue, _ = _build_adjuster(kill_switch_handler=handler)
    # SL pipeline off; only the kill-switch is armed.
    position = _arm_kill_switch(
        _build_long_position(trailing=False), price_move_pct=5.0
    )
    # Bar with an -8% move (100000 → 92000) ⇒ |8| >= 5 ⇒ trip.
    await adjuster.on_tick(
        _kline(open_time=1, open_=100_000, high=100_000, low=92_000, close=92_000),
        [position],
    )
    assert len(handler.calls) == 1
    pos_id, signal = handler.calls[0]
    assert pos_id == "p-1"
    assert signal.reason == "price_move"
    # The kill-switch did not also enqueue an SL adjustment for this position.
    assert queue.tasks == []


async def test_kill_switch_disabled_does_not_fire() -> None:
    handler = _RecordingKillSwitch()
    adjuster, _, _ = _build_adjuster(kill_switch_handler=handler)
    position = _build_long_position(trailing=False)  # kill_switch_enabled defaults False
    position.kill_switch_price_move_pct = 5.0  # threshold set but switch OFF
    await adjuster.on_tick(
        _kline(open_time=1, open_=100_000, high=100_000, low=92_000, close=92_000),
        [position],
    )
    assert handler.calls == []


async def test_kill_switch_no_handler_is_noop() -> None:
    # No handler wired (the production default) ⇒ never raises; SL path unaffected.
    adjuster, queue, _ = _build_adjuster()  # kill_switch_handler=None
    position = _arm_kill_switch(_build_long_position(), price_move_pct=5.0)
    adjusted = await adjuster.on_tick(
        _kline(open_time=1, open_=101_000, high=102_000, low=100_500, close=102_000),
        [position],
    )
    # The trailing SL still works exactly as before (handler absent is a pure no-op).
    assert adjusted == ["p-1"]
    assert len(queue.tasks) == 1


def test_atr_baseline_excludes_current_bar() -> None:
    # Review S3 — the kill-switch baseline is the mean ATR EXCLUDING the current
    # (last) bar, so a spike on the latest bar doesn't inflate its own baseline.
    adjuster, _, _ = _build_adjuster()
    for index in range(19):
        adjuster.update_buffer(
            _kline(open_time=index, open_=100.0, high=101.0, low=99.0, close=100.0)
        )
    adjuster.update_buffer(  # final bar: a volatility spike
        _kline(open_time=19, open_=100.0, high=140.0, low=60.0, close=130.0)
    )
    current, baseline = adjuster._atr_current_and_baseline(14)
    assert current is not None and baseline is not None

    df = pd.DataFrame(adjuster.buffer)
    series = ta.atr(df["high"], df["low"], df["close"], length=14).dropna()
    assert current == pytest.approx(float(series.iloc[-1]))
    assert baseline == pytest.approx(float(series.iloc[:-1].mean()))  # excludes last
    assert baseline != pytest.approx(float(series.mean()))  # NOT the full-buffer mean (old)


async def test_atr_computed_once_per_tick_for_kill_switch_and_sl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review S5 — a position with BOTH volatility-SL and the kill-switch (same ATR
    # period) computes ta.atr only ONCE per tick (memoized), not twice.
    real_atr = lt_module.ta.atr
    calls = {"n": 0}

    def _counting_atr(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return real_atr(*args, **kwargs)

    monkeypatch.setattr(lt_module.ta, "atr", _counting_atr)

    handler = _RecordingKillSwitch()
    adjuster, _, _ = _build_adjuster(kill_switch_handler=handler)
    for index in range(20):  # stable buffer (kill-switch will NOT trip)
        adjuster.update_buffer(
            _kline(open_time=index, open_=100_000.0, high=100_500.0, low=99_500.0, close=100_000.0)
        )
    position = _build_long_position(trailing=False, volatility=True, current_sl_price=80_000.0)
    position.kill_switch_enabled = True
    position.kill_switch_atr_spike_mult = 3.0  # period defaults to volatility's 14

    calls["n"] = 0  # count only the on_tick computation
    await adjuster.on_tick(
        _kline(open_time=21, open_=100_000, high=100_500, low=99_500, close=100_000),
        [position],
    )
    assert calls["n"] == 1  # one ta.atr for period 14, shared by kill-switch + SL eval
    assert handler.calls == []  # stable ⇒ no spike trip


async def test_kill_switch_cooldown_fires_once() -> None:
    state, now = _build_clock()
    handler = _RecordingKillSwitch()
    adjuster, _, _ = _build_adjuster(
        kill_switch_handler=handler, time_source=now, throttle_seconds=3.0
    )
    position = _arm_kill_switch(
        _build_long_position(trailing=False), price_move_pct=5.0, cooldown=10
    )
    await adjuster.on_tick(
        _kline(open_time=1, open_=100_000, high=100_000, low=92_000, close=92_000),
        [position],
    )
    state[0] += 2.0  # within the 10s kill-switch cooldown
    await adjuster.on_tick(
        _kline(open_time=2, open_=92_000, high=92_000, low=84_000, close=84_000),
        [position],
    )
    assert len(handler.calls) == 1
