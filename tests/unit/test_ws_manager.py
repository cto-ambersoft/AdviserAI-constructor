"""Unit tests for WebSocketManager data guards and reconnect sync."""

from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import (  # noqa: E402
    ConditionalOrderResult,
    ExchangeAdapter,
    PositionSide as ExchangePositionSide,
    PositionSnapshot,
)
from app.services.position.context import PositionContext, PositionSide, TPLevel  # noqa: E402
from app.services.position.order_queue import OrderPriority  # noqa: E402
from app.services.position.state_machine import PositionState, TransitionTrigger  # noqa: E402
from app.services.ws.manager import WebSocketManager  # noqa: E402


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _RecordingQueue:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    async def enqueue(self, task: object) -> None:
        self.tasks.append(task)


def _build_manager(
    adapter: AsyncMock,
    *,
    account_id: str = "acc-1",
    persist_position: AsyncMock | None = None,
    order_queue: _RecordingQueue | None = None,
) -> WebSocketManager:
    return WebSocketManager(
        adapter=adapter,
        account_id=account_id,
        persist_position=persist_position,
        order_queue_resolver=(lambda _position: order_queue) if order_queue is not None else None,
    )


def _build_position(
    *,
    symbol: str = "BTCUSDT",
    state: PositionState = PositionState.OPEN,
    quantity: float = 1.0,
    sl_price: float = 99000.0,
    side: PositionSide = PositionSide.LONG,
) -> PositionContext:
    return PositionContext(
        position_id="pos-1",
        account_id="acc-1",
        symbol=symbol,
        state=state,
        current_quantity=quantity,
        current_sl_price=sl_price,
        side=side,
    )


def _build_multi_tp_position(
    *,
    symbol: str = "BTC/USDT:USDT",
    state: PositionState,
    quantity: float = 1.0,
    tp_status: str = "open",
) -> PositionContext:
    return PositionContext(
        position_id="pos-mtp-1",
        account_id="acc-1",
        symbol=symbol,
        state=state,
        side=PositionSide.LONG,
        entry_price=100000.0,
        original_quantity=1.0,
        current_quantity=quantity,
        current_sl_price=98000.0,
        sl_exchange_order_id="sl-1",
        tp_mode="multi",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=33.0,
                trigger_price=101000.0,
                status=tp_status,
                exchange_order_id=None if tp_status == "pending" else "tp-1",
                move_sl_to="breakeven",
            ),
            TPLevel(
                level=2,
                price_offset_pct=2.0,
                close_pct=33.0,
                trigger_price=102000.0,
                status=tp_status,
                exchange_order_id=None if tp_status == "pending" else "tp-2",
                move_sl_to="tp1",
            ),
            TPLevel(
                level=3,
                price_offset_pct=3.0,
                close_pct=34.0,
                trigger_price=103000.0,
                status=tp_status,
                exchange_order_id=None if tp_status == "pending" else "tp-3",
            ),
        ],
    )


def _build_snapshot(
    *,
    symbol: str = "BTCUSDT",
    size: float = 1.0,
    mark_price: float = 100500.0,
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        side=ExchangePositionSide.LONG,
        size=size,
        entry_price=100000.0,
        unrealized_pnl=500.0,
        leverage=10,
        mark_price=mark_price,
        liquidation_price=90000.0,
        open_orders=[],
    )


@pytest.mark.asyncio
async def test_order_update_during_warmup_is_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("app.services.ws.manager.time.time", clock.time)

    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)
    position = _build_position()
    manager.track_position(position)

    route = AsyncMock()
    monkeypatch.setattr(manager, "_route_to_position", route)

    await manager.start()
    clock.advance(2.0)
    await manager._handle_order_update({"symbol": position.symbol, "price": 100100.0})

    route.assert_not_awaited()
    assert manager._last_prices[position.symbol] == pytest.approx(100100.0)


@pytest.mark.asyncio
async def test_order_update_after_warmup_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("app.services.ws.manager.time.time", clock.time)

    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)
    position = _build_position()
    manager.track_position(position)

    route = AsyncMock()
    monkeypatch.setattr(manager, "_route_to_position", route)

    await manager.start()
    clock.advance(5.0)
    event = {"symbol": position.symbol, "price": 100500.0}
    await manager._handle_order_update(event)

    route.assert_awaited_once_with(position, event)


@pytest.mark.asyncio
async def test_entry_fill_event_transitions_to_open_and_enqueues_initial_protection() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = _RecordingQueue()
    persisted_payloads: list[dict[str, object]] = []

    async def _persist(position: PositionContext) -> None:
        persisted_payloads.append(position.to_db_dict())

    manager = _build_manager(
        adapter,
        persist_position=AsyncMock(side_effect=_persist),
        order_queue=queue,
    )
    manager._warmed_up = True

    position = _build_multi_tp_position(state=PositionState.ENTERING, tp_status="pending")
    manager.track_position(position)

    await manager._handle_order_update(
        {
            "symbol": "BTCUSDT",
            "event_type": "ORDER_TRADE_UPDATE",
            "order_type": "market",
            "status": "filled",
            "order_id": "entry-1",
            "client_order_id": "client-entry-1",
            "filled_quantity": 1.0,
            "average_price": 100100.0,
            "reduce_only": False,
        }
    )

    assert position.state == PositionState.OPEN
    assert position.entry_price == pytest.approx(100100.0)
    assert [task.action for task in queue.tasks] == ["place_sl", "place_tp", "place_tp", "place_tp"]
    assert queue.tasks[0].params["symbol"] == "BTC/USDT:USDT"
    assert persisted_payloads[-1]["transition_log_json"][-1]["trigger"] == TransitionTrigger.ENTRY_FILLED


@pytest.mark.asyncio
async def test_tp_trigger_event_updates_history_partial_close_and_persists() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = _RecordingQueue()
    persisted_payloads: list[dict[str, object]] = []

    async def _persist(position: PositionContext) -> None:
        persisted_payloads.append(position.to_db_dict())

    manager = _build_manager(
        adapter,
        persist_position=AsyncMock(side_effect=_persist),
        order_queue=queue,
    )
    manager._warmed_up = True

    position = _build_multi_tp_position(state=PositionState.OPEN, quantity=1.0)
    manager.track_position(position)

    await manager._handle_order_update(
        {
            "symbol": "BTCUSDT",
            "event_type": "ALGO_UPDATE",
            "order_type": "take_profit",
            "status": "triggered",
            "order_id": "tp-1",
            "trigger_price": 101000.0,
            "filled_quantity": 0.33,
        }
    )

    assert position.state == PositionState.OPEN
    assert position.current_quantity == pytest.approx(0.67)
    assert len(position.tp_history) == 1
    assert position.tp_history[0].tp_level == 0
    assert len(queue.tasks) == 1
    assert queue.tasks[0].action == "replace_sl"
    assert persisted_payloads[-1]["tp_history_json"][0]["tp_level"] == 0
    assert persisted_payloads[-1]["transition_log_json"][-1]["trigger"] == TransitionTrigger.PARTIAL_CLOSE


@pytest.mark.asyncio
async def test_sl_trigger_event_closes_position_and_persists_sl_history() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    persisted_payloads: list[dict[str, object]] = []

    async def _persist(position: PositionContext) -> None:
        persisted_payloads.append(position.to_db_dict())

    manager = _build_manager(adapter, persist_position=AsyncMock(side_effect=_persist))
    manager._warmed_up = True

    position = _build_multi_tp_position(state=PositionState.OPEN, quantity=1.0)
    manager.track_position(position)

    await manager._handle_order_update(
        {
            "symbol": "BTCUSDT",
            "event_type": "execution",
            "order_type": "stop_loss",
            "status": "triggered",
            "order_id": "sl-live-2",
            "trigger_price": 98000.0,
            "filled_quantity": 1.0,
        }
    )

    assert position.state == PositionState.CLOSED
    assert position.current_quantity == pytest.approx(0.0)
    assert len(position.sl_history) == 1
    assert position.sl_history[0].new_price == pytest.approx(98000.0)
    assert [entry["trigger"] for entry in persisted_payloads[-1]["transition_log_json"]] == [
        TransitionTrigger.SL_TRIGGERED,
        TransitionTrigger.ALL_CLOSED,
    ]
    assert len(persisted_payloads[-1]["sl_history_json"]) == 1


@pytest.mark.asyncio
async def test_position_update_external_close_cancels_remaining_orders_and_persists() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.get_open_conditional_orders = AsyncMock(
        return_value=[
            ConditionalOrderResult(
                exchange_order_id="sl-1",
                client_order_id="cli-sl-1",
                order_type="stop_loss",
                trigger_price=98000.0,
                quantity=1.0,
                status="new",
            ),
            ConditionalOrderResult(
                exchange_order_id="tp-1",
                client_order_id="cli-tp-1",
                order_type="take_profit",
                trigger_price=101000.0,
                quantity=0.33,
                status="new",
            ),
        ]
    )
    adapter.cancel_conditional_order = AsyncMock(return_value=True)
    persisted_payloads: list[dict[str, object]] = []

    async def _persist(position: PositionContext) -> None:
        persisted_payloads.append(position.to_db_dict())

    manager = _build_manager(adapter, persist_position=AsyncMock(side_effect=_persist))
    position = _build_multi_tp_position(state=PositionState.OPEN, quantity=1.0)
    manager.track_position(position)

    await manager._handle_position_update({"symbol": "BTCUSDT", "size": 0.0, "reason": "external_close"})

    assert position.state == PositionState.CLOSED
    assert position.current_quantity == pytest.approx(0.0)
    cancelled_order_ids = {call.args[1] for call in adapter.cancel_conditional_order.await_args_list}
    assert cancelled_order_ids == {"sl-1", "tp-1", "tp-2", "tp-3"}
    assert position.sl_exchange_order_id is None
    assert [level.status for level in position.tp_levels] == ["cancelled", "cancelled", "cancelled"]
    assert [entry["trigger"] for entry in persisted_payloads[-1]["transition_log_json"]] == [
        TransitionTrigger.MANUAL_CLOSE,
        TransitionTrigger.ALL_CLOSED,
    ]


@pytest.mark.asyncio
async def test_invalid_transition_logs_and_attempts_state_sync_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)
    manager._warmed_up = True

    position = _build_multi_tp_position(state=PositionState.PENDING, quantity=1.0)
    manager.track_position(position)

    attempt_state_sync = AsyncMock()
    monkeypatch.setattr(manager, "_attempt_state_sync", attempt_state_sync)

    await manager._handle_order_update(
        {
            "symbol": "BTCUSDT",
            "event_type": "execution",
            "order_type": "stop_loss",
            "status": "triggered",
            "order_id": "sl-live-2",
            "trigger_price": 98000.0,
            "filled_quantity": 1.0,
        }
    )

    attempt_state_sync.assert_awaited_once()
    assert position.state == PositionState.PENDING
    assert position.current_quantity == pytest.approx(1.0)


def test_check_stale_tick_rejects_large_delta() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)
    manager._last_prices["BTCUSDT"] = 100000.0

    assert manager._check_stale_tick("BTCUSDT", 120000.0) is False
    assert manager._last_prices["BTCUSDT"] == pytest.approx(120000.0)


def test_check_stale_tick_accepts_normal_delta() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)
    manager._last_prices["BTCUSDT"] = 100000.0

    assert manager._check_stale_tick("BTCUSDT", 100500.0) is True
    assert manager._last_prices["BTCUSDT"] == pytest.approx(100500.0)


def test_check_stale_tick_accepts_first_symbol_tick() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)

    assert manager._check_stale_tick("SOLUSDT", 140.0) is True
    assert manager._last_prices["SOLUSDT"] == pytest.approx(140.0)


def test_check_stale_tick_recovers_on_next_good_tick_after_rejection() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)
    manager._last_prices["BTCUSDT"] = 100200.0

    assert manager._check_stale_tick("BTCUSDT", 80000.0) is False
    assert manager._last_prices["BTCUSDT"] == pytest.approx(80000.0)
    assert manager._last_good_prices["BTCUSDT"] == pytest.approx(100200.0)

    assert manager._check_stale_tick("BTCUSDT", 100300.0) is True
    assert manager._last_prices["BTCUSDT"] == pytest.approx(100300.0)
    assert manager._last_good_prices["BTCUSDT"] == pytest.approx(100300.0)


@pytest.mark.asyncio
async def test_update_heartbeat_tracks_ema_and_triggers_health_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("app.services.ws.manager.time.time", clock.time)

    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)
    manager._connected = True
    manager._last_heartbeat_at = clock.time()

    reconnect = AsyncMock()
    monkeypatch.setattr(manager, "_handle_disconnect", reconnect)

    for _ in range(3):
        clock.advance(1.0)
        manager.update_heartbeat()

    assert manager._jitter_ema_ms == pytest.approx(1000.0, rel=0.05)

    clock.advance(10.0)
    manager.update_heartbeat()

    assert manager._jitter_ema_ms > 1000.0

    await manager._check_connection_health()

    reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_health_respects_proactive_reconnect_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("app.services.ws.manager.time.time", clock.time)

    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = _build_manager(adapter)
    manager._connected = True
    manager._jitter_ema_ms = manager.JITTER_UNHEALTHY_MS + 100.0

    reconnect = AsyncMock()
    monkeypatch.setattr(manager, "_handle_disconnect", reconnect)

    await manager._check_connection_health()
    clock.advance(5.0)
    await manager._check_connection_health()

    reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_state_sync_closes_position_when_exchange_size_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.get_position = AsyncMock(return_value=_build_snapshot(size=0.0))
    adapter.get_open_conditional_orders = AsyncMock(return_value=[])

    manager = _build_manager(adapter)
    position = _build_position(state=PositionState.RECONNECTING, quantity=1.25)
    manager.track_position(position)

    persist = AsyncMock()
    monkeypatch.setattr(manager, "_persist_position", persist)

    await manager._full_state_sync()

    assert position.state == PositionState.CLOSED
    assert position.current_quantity == pytest.approx(0.0)
    adapter.get_open_conditional_orders.assert_not_awaited()
    persist.assert_awaited_once_with(position)


@pytest.mark.asyncio
async def test_full_state_sync_enqueues_emergency_sl_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.get_position = AsyncMock(return_value=_build_snapshot(size=1.5, mark_price=100750.0))
    adapter.get_open_conditional_orders = AsyncMock(return_value=[])

    manager = _build_manager(adapter)
    position = _build_position(state=PositionState.RECONNECTING, quantity=1.5, sl_price=99250.0)
    manager.track_position(position)

    queue = AsyncMock()
    queue.enqueue = AsyncMock()
    monkeypatch.setattr(manager, "_get_order_queue", AsyncMock(return_value=queue))
    monkeypatch.setattr(manager, "_persist_position", AsyncMock())

    await manager._full_state_sync()

    queue.enqueue.assert_awaited_once()
    task = queue.enqueue.await_args.args[0]
    assert task.priority == OrderPriority.EMERGENCY_SL
    assert task.action == "place_sl"
    assert task.params["symbol"] == position.symbol
    assert task.params["trigger_price"] == pytest.approx(position.current_sl_price)


@pytest.mark.asyncio
async def test_full_state_sync_seeds_last_prices_from_mark_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.get_position = AsyncMock(return_value=_build_snapshot(size=2.0, mark_price=101234.5))
    adapter.get_open_conditional_orders = AsyncMock(
        return_value=[
            ConditionalOrderResult(
                exchange_order_id="sl-1",
                client_order_id="cli-1",
                order_type="stop_loss",
                trigger_price=99000.0,
                quantity=2.0,
                status="new",
            )
        ]
    )

    manager = _build_manager(adapter)
    position = _build_position(state=PositionState.RECONNECTING, quantity=2.0)
    manager.track_position(position)

    monkeypatch.setattr(manager, "_persist_position", AsyncMock())

    await manager._full_state_sync()

    assert manager._last_prices[position.symbol] == pytest.approx(101234.5)


# ─── Realtime SL pipeline (trailing / breakeven / volatility) ────────────────


def _build_pipeline_position(
    *,
    position_id: str = "pos-rt-1",
    symbol: str = "BTC/USDT:USDT",
    trailing: bool = True,
    breakeven: bool = False,
    volatility: bool = False,
    sl_order_id: str | None = "sl-rt-1",
    state: PositionState = PositionState.OPEN,
) -> PositionContext:
    return PositionContext(
        position_id=position_id,
        account_id="acc-1",
        symbol=symbol,
        state=state,
        side=PositionSide.LONG,
        entry_price=100_000.0,
        original_quantity=0.5,
        current_quantity=0.5,
        current_sl_price=98_000.0,
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


@pytest.mark.asyncio
async def test_entry_fill_subscribes_kline_for_pipeline_position() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = _RecordingQueue()
    manager = _build_manager(
        adapter,
        persist_position=AsyncMock(),
        order_queue=queue,
    )
    manager._warmed_up = True
    position = _build_pipeline_position(state=PositionState.ENTERING)
    manager.track_position(position)

    await manager._handle_order_update(
        {
            "symbol": "BTC/USDT:USDT",
            "event_type": "ORDER_TRADE_UPDATE",
            "order_type": "market",
            "status": "filled",
            "order_id": "entry-1",
            "client_order_id": "client-entry-1",
            "filled_quantity": 0.5,
            "average_price": 100_000.0,
            "reduce_only": False,
        }
    )

    adapter.subscribe_kline.assert_awaited_once()
    kwargs = adapter.subscribe_kline.await_args.kwargs
    assert kwargs["symbol"] == "BTC/USDT:USDT"
    assert kwargs["interval"] == manager.SL_PIPELINE_KLINE_INTERVAL
    assert callable(kwargs["on_kline"])
    assert "BTC/USDT:USDT" in manager._sl_adjusters


@pytest.mark.asyncio
async def test_entry_fill_skips_kline_subscription_when_pipeline_disabled() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = _RecordingQueue()
    manager = _build_manager(
        adapter,
        persist_position=AsyncMock(),
        order_queue=queue,
    )
    manager._warmed_up = True
    position = _build_pipeline_position(
        state=PositionState.ENTERING,
        trailing=False,
        breakeven=False,
        volatility=False,
    )
    manager.track_position(position)

    await manager._handle_order_update(
        {
            "symbol": "BTC/USDT:USDT",
            "event_type": "ORDER_TRADE_UPDATE",
            "order_type": "market",
            "status": "filled",
            "order_id": "entry-1",
            "client_order_id": "client-entry-1",
            "filled_quantity": 0.5,
            "average_price": 100_000.0,
            "reduce_only": False,
        }
    )

    adapter.subscribe_kline.assert_not_awaited()
    assert manager._sl_adjusters == {}


@pytest.mark.asyncio
async def test_second_position_on_same_symbol_reuses_kline_subscription() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = _RecordingQueue()
    manager = _build_manager(
        adapter,
        persist_position=AsyncMock(),
        order_queue=queue,
    )
    manager._warmed_up = True

    first = _build_pipeline_position(position_id="pos-a", state=PositionState.OPEN)
    manager.track_position(first)
    await manager._ensure_realtime_sl_pipeline(first)
    assert adapter.subscribe_kline.await_count == 1

    second = _build_pipeline_position(position_id="pos-b", state=PositionState.OPEN)
    manager.track_position(second)
    await manager._ensure_realtime_sl_pipeline(second)

    # Same symbol → no second subscribe.
    assert adapter.subscribe_kline.await_count == 1


@pytest.mark.asyncio
async def test_untrack_position_clears_realtime_pipeline_when_last() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = _RecordingQueue()
    manager = _build_manager(adapter, persist_position=AsyncMock(), order_queue=queue)
    position = _build_pipeline_position()
    manager.track_position(position)
    await manager._ensure_realtime_sl_pipeline(position)
    assert "BTC/USDT:USDT" in manager._sl_adjusters

    manager.untrack_position(position.symbol)

    assert manager._sl_adjusters == {}


@pytest.mark.asyncio
async def test_realtime_kline_routes_to_adjuster_and_enqueues_replace_sl() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = _RecordingQueue()
    manager = _build_manager(adapter, persist_position=AsyncMock(), order_queue=queue)

    position = _build_pipeline_position()
    manager.track_position(position)
    await manager._ensure_realtime_sl_pipeline(position)

    await manager._handle_realtime_kline(
        position.symbol,
        {
            "open_time": 1,
            "open": 101_000.0,
            "high": 102_000.0,
            "low": 100_500.0,
            "close": 102_000.0,
            "volume": 1.0,
            "is_closed": True,
        },
    )

    assert len(queue.tasks) == 1
    task = queue.tasks[0]
    assert task.action == "replace_sl"
    assert task.priority == OrderPriority.SL_ADJUSTMENT
    assert task.params["existing_order_id"] == "sl-rt-1"
    # trailing: 102000 * 0.99 = 100980
    assert task.params["new_trigger_price"] == pytest.approx(100_980.0)


@pytest.mark.asyncio
async def test_realtime_kline_no_adjuster_for_unknown_symbol_is_noop() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = _RecordingQueue()
    manager = _build_manager(adapter, persist_position=AsyncMock(), order_queue=queue)

    await manager._handle_realtime_kline(
        "UNKNOWN/USDT",
        {
            "open_time": 1,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "is_closed": True,
        },
    )

    assert queue.tasks == []
