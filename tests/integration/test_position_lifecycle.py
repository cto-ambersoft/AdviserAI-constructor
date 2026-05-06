"""Integration tests for end-to-end position lifecycle orchestration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import (  # noqa: E402
    ConditionalOrderResult,
    EntryOrderResult,
    ExchangeAdapter,
    OrderSide,
    PartialCloseResult,
    PositionSide as ExchangePositionSide,
    PositionSnapshot,
    RateLimitState,
)
from app.services.position.context import PositionContext, PositionSide, TPLevel  # noqa: E402
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
    TransientExchangeError,
)
from app.services.position.state_machine import PositionState, TransitionTrigger  # noqa: E402
from app.services.sl_tp.pipeline import SLAdjustmentPipeline  # noqa: E402
from app.services.ws.manager import WebSocketManager  # noqa: E402


def _event_symbol(symbol: str) -> str:
    return symbol.split(":", 1)[0].replace("/", "").replace("-", "").replace("_", "").upper()


def _exchange_side(side: PositionSide) -> ExchangePositionSide:
    if side == PositionSide.SHORT:
        return ExchangePositionSide.SHORT
    return ExchangePositionSide.LONG


class _FakeExchangeAdapter(ExchangeAdapter):
    def __init__(self) -> None:
        self._order_counter = 0
        self._open_orders: dict[str, dict[str, ConditionalOrderResult]] = {}
        self._positions: dict[str, dict[str, Any]] = {}
        self.cancelled_orders: list[tuple[str, str]] = []
        self.partial_close_calls: list[dict[str, Any]] = []
        self.placed_orders: list[ConditionalOrderResult] = []
        self.stop_loss_failures_remaining = 0
        self.replace_sl_failures_remaining = 0
        self.auto_emit_position_updates = False
        self.subscriptions = 0
        self._on_order_update: Any = None
        self._on_position_update: Any = None
        self._on_disconnect: Any = None

    def set_position(
        self,
        *,
        symbol: str,
        size: float,
        side: ExchangePositionSide = ExchangePositionSide.LONG,
        entry_price: float = 100000.0,
        mark_price: float = 100500.0,
    ) -> None:
        current = self._positions.get(symbol, {})
        self._positions[symbol] = {
            "size": float(size),
            "side": side,
            "entry_price": float(entry_price),
            "mark_price": float(mark_price),
            "unrealized_pnl": float(current.get("unrealized_pnl", 500.0)),
            "leverage": int(current.get("leverage", 10)),
            "liquidation_price": float(current.get("liquidation_price", 90000.0)),
        }

    def find_order(self, symbol: str, order_type: str) -> ConditionalOrderResult:
        for order in self._open_orders.get(symbol, {}).values():
            if order.order_type == order_type:
                return order
        raise LookupError(f"Order type {order_type} not found for {symbol}")

    def mark_order_closed(self, symbol: str, order_id: str) -> None:
        self._open_orders.get(symbol, {}).pop(order_id, None)

    async def get_position(self, symbol: str) -> PositionSnapshot | None:
        payload = self._positions.get(symbol)
        if payload is None:
            return None

        return PositionSnapshot(
            symbol=symbol,
            side=payload["side"],
            size=float(payload["size"]),
            entry_price=float(payload["entry_price"]),
            unrealized_pnl=float(payload["unrealized_pnl"]),
            leverage=int(payload["leverage"]),
            mark_price=float(payload["mark_price"]),
            liquidation_price=float(payload["liquidation_price"]),
            open_orders=list(self._open_orders.get(symbol, {}).values()),
        )

    async def get_open_conditional_orders(self, symbol: str) -> list[ConditionalOrderResult]:
        return list(self._open_orders.get(symbol, {}).values())

    async def place_entry_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        client_order_id: str,
    ) -> EntryOrderResult:
        return EntryOrderResult(
            exchange_order_id="entry-1",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            order_type="market",
            status="filled",
            quantity=quantity,
            filled_quantity=quantity,
            remaining_quantity=0.0,
            price=None,
            average_price=100000.0,
            cost=quantity * 100000.0,
            timestamp=datetime.now(UTC),
            raw={},
        )

    async def place_stop_loss(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        reduce_only: bool = True,
    ) -> ConditionalOrderResult:
        _ = (side, reduce_only)
        if self.stop_loss_failures_remaining > 0:
            self.stop_loss_failures_remaining -= 1
            raise TransientExchangeError("simulated stop-loss placement failure")
        return self._register_order(
            symbol=symbol,
            order_type="stop_loss",
            trigger_price=trigger_price,
            quantity=quantity,
            client_order_id=client_order_id,
        )

    async def place_take_profit(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        reduce_only: bool = True,
        limit_price: float | None = None,
    ) -> ConditionalOrderResult:
        _ = (side, reduce_only, limit_price)
        return self._register_order(
            symbol=symbol,
            order_type="take_profit",
            trigger_price=trigger_price,
            quantity=quantity,
            client_order_id=client_order_id,
        )

    async def place_trailing_stop(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        callback_rate: float,
        activation_price: float | None,
        client_order_id: str,
    ) -> ConditionalOrderResult:
        _ = (side, callback_rate, activation_price)
        return self._register_order(
            symbol=symbol,
            order_type="trailing_stop",
            trigger_price=0.0,
            quantity=quantity,
            client_order_id=client_order_id,
        )

    async def cancel_and_replace_sl(
        self,
        symbol: str,
        existing_order_id: str,
        new_trigger_price: float,
        new_quantity: float,
        client_order_id: str,
    ) -> ConditionalOrderResult:
        if self.replace_sl_failures_remaining > 0:
            self.replace_sl_failures_remaining -= 1
            raise TransientExchangeError("simulated stop-loss replacement failure")

        self.mark_order_closed(symbol, existing_order_id)
        return self._register_order(
            symbol=symbol,
            order_type="stop_loss",
            trigger_price=new_trigger_price,
            quantity=new_quantity,
            client_order_id=client_order_id,
        )

    async def cancel_and_replace_tp(
        self,
        symbol: str,
        existing_order_id: str,
        new_trigger_price: float,
        new_quantity: float,
        client_order_id: str,
        limit_price: float | None = None,
    ) -> ConditionalOrderResult:
        _ = limit_price
        self.mark_order_closed(symbol, existing_order_id)
        return self._register_order(
            symbol=symbol,
            order_type="take_profit",
            trigger_price=new_trigger_price,
            quantity=new_quantity,
            client_order_id=client_order_id,
        )

    async def cancel_conditional_order(self, symbol: str, order_id: str) -> bool:
        self.cancelled_orders.append((symbol, order_id))
        self.mark_order_closed(symbol, order_id)
        return True

    async def partial_close(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        client_order_id: str,
        order_type: str = "market",
        price: float | None = None,
    ) -> PartialCloseResult:
        _ = (side, price)
        payload = self._positions.setdefault(
            symbol,
            {
                "size": quantity,
                "side": ExchangePositionSide.LONG,
                "entry_price": 100000.0,
                "mark_price": 100000.0,
                "unrealized_pnl": 0.0,
                "leverage": 10,
                "liquidation_price": 90000.0,
            },
        )
        remaining = max(float(payload["size"]) - float(quantity), 0.0)
        payload["size"] = remaining

        call = {
            "symbol": symbol,
            "quantity": float(quantity),
            "client_order_id": client_order_id,
            "order_type": order_type,
            "remaining": remaining,
        }
        self.partial_close_calls.append(call)

        if self.auto_emit_position_updates and self._on_position_update is not None:
            await self._on_position_update(
                {
                    "symbol": _event_symbol(symbol),
                    "size": remaining,
                    "reason": "emergency_close",
                }
            )

        return PartialCloseResult(
            executed_qty=float(quantity),
            avg_price=float(payload["mark_price"]),
            remaining_qty=remaining,
            order_id=f"close-{len(self.partial_close_calls)}",
            commission=0.0,
        )

    async def subscribe_user_data(
        self,
        on_order_update: Any,
        on_position_update: Any,
        on_disconnect: Any,
    ) -> None:
        self.subscriptions += 1
        self._on_order_update = on_order_update
        self._on_position_update = on_position_update
        self._on_disconnect = on_disconnect

    async def subscribe_kline(
        self,
        symbol: str,
        interval: str,
        on_kline: Any,
    ) -> None:
        _ = (symbol, interval, on_kline)

    async def get_rate_limit_state(self) -> RateLimitState:
        return RateLimitState(
            order_count_10s=0,
            order_count_1m=0,
            order_limit_10s=300,
            order_limit_1m=1200,
            weight_used_1m=0,
            weight_limit_1m=2400,
            retry_after=None,
        )

    async def can_place_order(self) -> bool:
        return True

    def _register_order(
        self,
        *,
        symbol: str,
        order_type: str,
        trigger_price: float,
        quantity: float,
        client_order_id: str,
    ) -> ConditionalOrderResult:
        self._order_counter += 1
        prefix = {
            "stop_loss": "sl",
            "take_profit": "tp",
            "trailing_stop": "trail",
        }.get(order_type, "ord")
        result = ConditionalOrderResult(
            exchange_order_id=f"{prefix}-{self._order_counter}",
            client_order_id=client_order_id,
            order_type=order_type,
            trigger_price=float(trigger_price),
            quantity=float(quantity),
            status="new",
        )
        self._open_orders.setdefault(symbol, {})[result.exchange_order_id] = result
        self.placed_orders.append(result)
        return result


class _LifecycleHarness:
    def __init__(self) -> None:
        self.adapter = _FakeExchangeAdapter()
        self.persisted_payloads: list[dict[str, Any]] = []
        self.queue = OrderExecutionQueue(adapter=self.adapter, account_id="acc-1")
        self.manager = WebSocketManager(
            adapter=self.adapter,
            account_id="acc-1",
            persist_position=self._persist_position,
            order_queue_resolver=lambda _position: self.queue,
        )
        self._processor: asyncio.Task[None] | None = None

    async def start(self, *, start_ws: bool = False) -> None:
        self._processor = asyncio.create_task(self.queue.start_processing())
        if start_ws:
            await self.manager.start()
        else:
            self.manager._warmed_up = True

    async def stop(self) -> None:
        await self.queue.stop()
        if self._processor is not None:
            await asyncio.wait_for(self._processor, timeout=1.0)

    async def drain(self) -> None:
        await asyncio.wait_for(self.queue._queue.join(), timeout=1.0)

    def track_position(self, position: PositionContext, *, mark_price: float | None = None) -> None:
        self.manager.track_position(position)
        self.adapter.set_position(
            symbol=position.symbol,
            size=position.current_quantity,
            side=_exchange_side(position.side),
            entry_price=position.entry_price or 100000.0,
            mark_price=mark_price or position.entry_price or 100000.0,
        )

    async def place_initial_protection(self, position: PositionContext) -> None:
        await self.manager._enqueue_initial_protection_orders(position)
        await self.drain()

    async def emit_order(self, event: dict[str, Any]) -> None:
        position = self.manager._find_position(str(event.get("symbol", "") or ""))
        if position is not None:
            order_id = str(event.get("order_id", "") or "")
            if order_id and str(event.get("status", "")).lower() in {"filled", "triggered"}:
                self.adapter.mark_order_closed(position.symbol, order_id)
        await self.manager._handle_order_update(event)

    async def emit_position(self, event: dict[str, Any]) -> None:
        position = self.manager._find_position(str(event.get("symbol", "") or ""))
        if position is not None:
            self.adapter.set_position(
                symbol=position.symbol,
                size=abs(float(event.get("size", event.get("qty", 0.0)))),
                side=_exchange_side(position.side),
                entry_price=position.entry_price or 100000.0,
                mark_price=position.entry_price or 100000.0,
            )
        await self.manager._handle_position_update(event)

    async def _persist_position(self, position: PositionContext) -> None:
        self.persisted_payloads.append(position.to_db_dict())


def _build_single_tp_position(*, state: PositionState = PositionState.PENDING) -> PositionContext:
    return PositionContext(
        position_id="pos-single",
        account_id="acc-1",
        symbol="BTC/USDT:USDT",
        state=state,
        side=PositionSide.LONG,
        entry_price=100000.0,
        original_quantity=1.0,
        current_quantity=1.0,
        current_sl_price=98000.0,
        current_tp_price=102000.0,
        tp_mode="single",
    )


def _build_multi_tp_position(
    *,
    state: PositionState = PositionState.OPEN,
    quantity: float = 1.0,
) -> PositionContext:
    return PositionContext(
        position_id="pos-multi",
        account_id="acc-1",
        symbol="BTC/USDT:USDT",
        state=state,
        side=PositionSide.LONG,
        entry_price=100000.0,
        original_quantity=1.0,
        current_quantity=quantity,
        current_sl_price=98000.0,
        tp_mode="multi",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=33.0,
                trigger_price=101000.0,
                status="pending",
                exchange_order_id=None,
                move_sl_to="breakeven",
            ),
            TPLevel(
                level=2,
                price_offset_pct=2.0,
                close_pct=33.0,
                trigger_price=102000.0,
                status="pending",
                exchange_order_id=None,
                move_sl_to="tp1",
            ),
            TPLevel(
                level=3,
                price_offset_pct=3.0,
                close_pct=34.0,
                trigger_price=103000.0,
                status="pending",
                exchange_order_id=None,
            ),
        ],
    )


def _build_trailing_position() -> PositionContext:
    return PositionContext(
        position_id="pos-trailing",
        account_id="acc-1",
        symbol="BTC/USDT:USDT",
        state=PositionState.OPEN,
        side=PositionSide.LONG,
        entry_price=100000.0,
        original_quantity=1.0,
        current_quantity=1.0,
        current_sl_price=98000.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
    )


def _attach_trailing_handler(
    harness: _LifecycleHarness,
    position: PositionContext,
) -> Any:
    async def _handle_order_update(event: dict[str, Any]) -> None:
        price = float(event["price"])
        pipeline = SLAdjustmentPipeline(position)
        result = await pipeline.evaluate(current_price=price, indicators={}, kline_data=[])
        if result is None:
            return

        if position.state_machine.can_transition(TransitionTrigger.TRAILING_TICK):
            position.state = position.state_machine.transition(
                TransitionTrigger.TRAILING_TICK,
                reason=result.detail,
                metadata={"price": price, "source": result.reason},
            )

        await harness.queue.enqueue(
            OrderTask(
                priority=OrderPriority.SL_ADJUSTMENT,
                created_at=time.time(),
                position_id=position.position_id,
                action="replace_sl",
                params={
                    "symbol": position.symbol,
                    "existing_order_id": position.sl_exchange_order_id or "missing-sl",
                    "new_trigger_price": result.new_sl_price,
                    "new_quantity": position.current_quantity,
                    "client_order_id": f"{position.position_id}-trail-{int(time.time() * 1000)}",
                    "reason": result.reason,
                },
                on_success=harness.manager._build_sl_adjustment_callback(
                    position=position,
                    reason=result.reason,
                    trigger_source="trailing_engine",
                    update_tracking=result.update_tracking,
                ),
            )
        )

    position.handle_order_update = _handle_order_update  # type: ignore[attr-defined]
    return _handle_order_update


@pytest.mark.asyncio
async def test_happy_path_single_tp_lifecycle() -> None:
    harness = _LifecycleHarness()
    await harness.start()
    try:
        position = _build_single_tp_position(state=PositionState.PENDING)
        harness.track_position(position)

        position.state = position.state_machine.transition(
            TransitionTrigger.ENTRY_SUBMITTED,
            reason="Entry submitted",
        )

        await harness.emit_order(
            {
                "symbol": _event_symbol(position.symbol),
                "event_type": "ORDER_TRADE_UPDATE",
                "order_type": "market",
                "status": "filled",
                "order_id": "entry-1",
                "client_order_id": "client-entry-1",
                "filled_quantity": 1.0,
                "average_price": 100000.0,
                "reduce_only": False,
            }
        )
        await harness.drain()

        sl_order = harness.adapter.find_order(position.symbol, "stop_loss")
        tp_order = harness.adapter.find_order(position.symbol, "take_profit")

        await harness.emit_order(
            {
                "symbol": _event_symbol(position.symbol),
                "event_type": "ALGO_UPDATE",
                "order_type": "take_profit",
                "status": "triggered",
                "order_id": tp_order.exchange_order_id,
                "trigger_price": 102000.0,
                "filled_quantity": 1.0,
            }
        )

        assert position.state == PositionState.CLOSED
        assert position.current_quantity == pytest.approx(0.0)
        assert position.sl_exchange_order_id is None
        assert [entry["trigger"] for entry in position.state_machine.get_transition_log()] == [
            TransitionTrigger.ENTRY_SUBMITTED,
            TransitionTrigger.ENTRY_FILLED,
            TransitionTrigger.TP_TRIGGERED,
            TransitionTrigger.ALL_CLOSED,
        ]
        assert {order.order_type for order in harness.adapter.placed_orders} == {
            "stop_loss",
            "take_profit",
        }
        assert harness.adapter.cancelled_orders == [(position.symbol, sl_order.exchange_order_id)]
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_multi_tp_lifecycle_tracks_quantity_and_histories() -> None:
    harness = _LifecycleHarness()
    await harness.start()
    try:
        position = _build_multi_tp_position()
        harness.track_position(position)
        await harness.place_initial_protection(position)

        sl_order = harness.adapter.find_order(position.symbol, "stop_loss")
        assert sl_order.exchange_order_id == position.sl_exchange_order_id
        assert [level.status for level in position.tp_levels] == ["open", "open", "open"]

        await harness.emit_order(
            {
                "symbol": _event_symbol(position.symbol),
                "event_type": "ALGO_UPDATE",
                "order_type": "take_profit",
                "status": "triggered",
                "order_id": position.tp_levels[0].exchange_order_id,
                "trigger_price": position.tp_levels[0].trigger_price,
                "filled_quantity": 0.33,
            }
        )
        await harness.drain()

        assert position.current_quantity == pytest.approx(0.67)
        assert position.current_sl_price == pytest.approx(position.entry_price)

        await harness.emit_order(
            {
                "symbol": _event_symbol(position.symbol),
                "event_type": "ALGO_UPDATE",
                "order_type": "take_profit",
                "status": "triggered",
                "order_id": position.tp_levels[1].exchange_order_id,
                "trigger_price": position.tp_levels[1].trigger_price,
                "filled_quantity": 0.33,
            }
        )
        await harness.drain()

        assert position.current_quantity == pytest.approx(0.34)
        assert position.current_sl_price == pytest.approx(position.tp_levels[0].trigger_price)

        await harness.emit_order(
            {
                "symbol": _event_symbol(position.symbol),
                "event_type": "ALGO_UPDATE",
                "order_type": "take_profit",
                "status": "triggered",
                "order_id": position.tp_levels[2].exchange_order_id,
                "trigger_price": position.tp_levels[2].trigger_price,
                "filled_quantity": 0.34,
            }
        )

        assert position.state == PositionState.CLOSED
        assert position.current_quantity == pytest.approx(0.0)
        assert len(position.sl_history) == 2
        assert len(position.tp_history) == 3
        assert [entry.new_price for entry in position.sl_history] == pytest.approx(
            [position.entry_price, position.tp_levels[0].trigger_price]
        )
        assert [entry.tp_level for entry in position.tp_history] == [0, 1, 2]
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_trailing_stop_lifecycle_records_shift_history() -> None:
    harness = _LifecycleHarness()
    await harness.start()
    try:
        position = _build_trailing_position()
        harness.manager.STALE_TICK_THRESHOLD_PCT = 0.05
        harness.track_position(position)
        await harness.place_initial_protection(position)
        _attach_trailing_handler(harness, position)

        await harness.emit_order({"symbol": _event_symbol(position.symbol), "price": 102000.0})
        await harness.drain()
        assert position.current_sl_price == pytest.approx(100980.0)

        await harness.emit_order({"symbol": _event_symbol(position.symbol), "price": 105000.0})
        await harness.drain()
        assert position.current_sl_price == pytest.approx(103950.0)

        await harness.emit_order(
            {
                "symbol": _event_symbol(position.symbol),
                "event_type": "execution",
                "order_type": "stop_loss",
                "status": "triggered",
                "order_id": position.sl_exchange_order_id,
                "trigger_price": 103950.0,
                "filled_quantity": 1.0,
            }
        )

        trailing_entries = [entry for entry in position.sl_history if entry.reason == "trailing"]
        assert position.state == PositionState.CLOSED
        assert len(trailing_entries) == 2
        assert [entry.new_price for entry in trailing_entries] == pytest.approx([100980.0, 103950.0])
        assert all(entry.trigger_source == "trailing_engine" for entry in trailing_entries)
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_stale_tick_guard_rejects_false_trailing_update_and_recovers() -> None:
    harness = _LifecycleHarness()
    await harness.start()
    try:
        position = _build_trailing_position()
        harness.track_position(position)
        await harness.place_initial_protection(position)
        trailing_handler = _attach_trailing_handler(harness, position)
        routed_handler = AsyncMock(side_effect=trailing_handler)
        position.handle_order_update = routed_handler  # type: ignore[attr-defined]

        for price in (100000.0, 100100.0, 100200.0):
            await harness.emit_order({"symbol": _event_symbol(position.symbol), "price": price})
            await harness.drain()

        assert position.trailing_highest_price == pytest.approx(100200.0)
        history_before_stale = len(position.sl_history)
        routed_before_stale = routed_handler.await_count

        await harness.emit_order({"symbol": _event_symbol(position.symbol), "price": 80000.0})
        await harness.drain()

        assert routed_handler.await_count == routed_before_stale
        assert len(position.sl_history) == history_before_stale
        assert position.trailing_highest_price == pytest.approx(100200.0)
        assert harness.manager._last_prices[_event_symbol(position.symbol)] == pytest.approx(80000.0)
        assert harness.manager._last_good_prices[_event_symbol(position.symbol)] == pytest.approx(100200.0)

        await harness.emit_order({"symbol": _event_symbol(position.symbol), "price": 100300.0})
        await harness.drain()

        assert routed_handler.await_count == routed_before_stale + 1
        assert position.trailing_highest_price == pytest.approx(100300.0)
        assert position.sl_history[-1].reason == "trailing"
        assert len(position.sl_history) == history_before_stale + 1
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_reconnect_state_sync_restores_open_state_and_keeps_warmup_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _LifecycleHarness()
    position = _build_single_tp_position(state=PositionState.OPEN)
    forwarded = AsyncMock()
    position.handle_order_update = forwarded  # type: ignore[attr-defined]
    harness.track_position(position, mark_price=100750.0)
    await harness.start(start_ws=True)
    try:
        await harness.place_initial_protection(position)
        harness.manager._warmed_up = True

        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("app.services.ws.manager.asyncio.sleep", sleep_mock)

        await harness.manager._handle_disconnect()

        assert position.state == PositionState.OPEN
        assert [entry["trigger"] for entry in position.state_machine.get_transition_log()] == [
            TransitionTrigger.WS_DISCONNECTED,
            TransitionTrigger.SYNC_COMPLETE,
        ]

        await harness.emit_order({"symbol": _event_symbol(position.symbol), "price": 100900.0})

        forwarded.assert_not_awaited()
        assert harness.manager._last_prices[_event_symbol(position.symbol)] == pytest.approx(100900.0)
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_sl_placement_failure_escalates_to_emergency_close() -> None:
    harness = _LifecycleHarness()
    position = _build_single_tp_position(state=PositionState.OPEN)
    position.current_tp_price = None
    harness.track_position(position)
    harness.adapter.stop_loss_failures_remaining = 4
    harness.adapter.auto_emit_position_updates = True
    await harness.start(start_ws=True)
    try:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("app.services.position.order_queue.asyncio.sleep", sleep_mock)
        await harness.manager._enqueue_initial_protection_orders(position)
        await harness.drain()

        assert position.state == PositionState.CLOSED
        assert position.current_quantity == pytest.approx(0.0)
        assert len(harness.adapter.partial_close_calls) == 1
        assert harness.adapter.partial_close_calls[0]["order_type"] == "market"
    finally:
        monkeypatch.undo()
        await harness.stop()


@pytest.mark.asyncio
async def test_external_close_cancels_remaining_orders() -> None:
    harness = _LifecycleHarness()
    await harness.start()
    try:
        position = _build_multi_tp_position()
        harness.track_position(position)
        await harness.place_initial_protection(position)

        known_order_ids = {
            position.sl_exchange_order_id,
            *(level.exchange_order_id for level in position.tp_levels),
        }

        await harness.emit_position(
            {
                "symbol": _event_symbol(position.symbol),
                "size": 0.0,
                "reason": "external_close",
            }
        )

        assert position.state == PositionState.CLOSED
        assert position.current_quantity == pytest.approx(0.0)
        assert position.sl_exchange_order_id is None
        assert [level.status for level in position.tp_levels] == ["cancelled", "cancelled", "cancelled"]
        assert {order_id for _, order_id in harness.adapter.cancelled_orders} == {
            order_id for order_id in known_order_ids if order_id is not None
        }
    finally:
        await harness.stop()
