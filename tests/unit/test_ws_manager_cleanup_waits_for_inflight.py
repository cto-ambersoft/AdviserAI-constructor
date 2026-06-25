"""Regression: ``_cancel_remaining_orders`` drains the queue before on-exchange DELETE.

Without ``await_quiescent``, the cleanup path raced ``cancel_and_replace_sl``
(place-first, cancel-last on Binance) — the on-exchange DELETE in the cleanup
deleted the *new* SL the queue task had just placed, leaving the position
unprotected.

This test confirms: when a queue task is mid-flight for the position,
``_cancel_remaining_orders`` waits for it before issuing any
``cancel_conditional_order`` calls.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import (  # noqa: E402
    ConditionalOrderResult,
    ExchangeAdapter,
    OrderSide,
    RateLimitState,
)
from app.services.position.context import (  # noqa: E402
    PositionContext,
    PositionSide,
    TPLevel,
)
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
)
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.ws.manager import WebSocketManager  # noqa: E402


def _rate_state() -> RateLimitState:
    return RateLimitState(
        order_count_10s=0,
        order_count_1m=0,
        order_limit_10s=300,
        order_limit_1m=1200,
        weight_used_1m=0,
        weight_limit_1m=2400,
        retry_after=None,
    )


def _build_position() -> PositionContext:
    return PositionContext(
        position_id="pos-cleanup-1",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        state=PositionState.OPEN,
        entry_price=100_000.0,
        original_quantity=1.0,
        current_quantity=0.0,
        current_sl_price=98_000.0,
        sl_exchange_order_id="sl-old",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=100.0,
                trigger_price=101_000.0,
                status="triggered",
                exchange_order_id="tp-1",
            ),
        ],
    )


async def test_cancel_remaining_orders_waits_for_inflight_replace_sl() -> None:
    # Build a real queue so we exercise its ``_executing_keys`` plumbing.
    queue_adapter = AsyncMock(spec=ExchangeAdapter)
    queue_adapter.can_place_order.return_value = True
    queue_adapter.get_rate_limit_state.return_value = _rate_state()
    # An in-flight ``cancel_and_replace_sl`` that blocks until we release.
    release = asyncio.Event()
    cancel_calls: list[str] = []

    async def _slow_replace(**_: Any) -> ConditionalOrderResult:
        await release.wait()
        return ConditionalOrderResult(
            exchange_order_id="sl-new",
            client_order_id="coid-1",
            order_type="stop_loss",
            trigger_price=100_500.0,
            quantity=1.0,
            status="new",
        )

    queue_adapter.cancel_and_replace_sl.side_effect = _slow_replace
    queue = OrderExecutionQueue(adapter=queue_adapter, account_id="acc-1")
    processor = asyncio.create_task(queue.start_processing())

    # WS manager adapter (separate — we only care about the DELETE call ordering).
    ws_adapter = AsyncMock(spec=ExchangeAdapter)
    ws_adapter.get_open_conditional_orders.return_value = []

    async def _track_cancel(symbol: str, order_id: str) -> bool:
        cancel_calls.append(order_id)
        return True

    ws_adapter.cancel_conditional_order.side_effect = _track_cancel

    async def _resolver(_position: PositionContext) -> OrderExecutionQueue:
        return queue

    manager = WebSocketManager(
        adapter=ws_adapter,
        account_id="acc-1",
        order_queue_resolver=_resolver,
    )

    position = _build_position()
    manager.track_position(position)

    # Enqueue a long-running replace_sl task.
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=1.0,
            position_id="pos-cleanup-1",
            action="replace_sl",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "existing_order_id": "sl-old",
                "new_trigger_price": 100_500.0,
                "trigger_price": 100_500.0,
                "new_quantity": 1.0,
                "full_quantity": 1.0,
                "client_order_id": "coid-1",
                "close_position": True,
                "reason": "tp1_hit_sl_adjustment",
            },
        )
    )
    await asyncio.sleep(0.05)  # let the queue dequeue and start executing

    cleanup_task = asyncio.create_task(
        manager._cancel_remaining_orders(position)  # type: ignore[attr-defined]
    )
    await asyncio.sleep(0.05)
    # The cleanup must NOT have called ``cancel_conditional_order`` yet —
    # ``await_quiescent`` is blocking on the in-flight replace_sl.
    assert cancel_calls == []
    assert not cleanup_task.done()

    release.set()
    await asyncio.wait_for(cleanup_task, timeout=2.0)

    # Now the DELETE has been issued (for whatever ids were known).
    # The key assertion is the *ordering*: cancel was deferred until replace
    # finished. We don't care about exact id contents — the queue task's
    # on_success callback updates ``sl_exchange_order_id`` to ``"sl-new"``.
    assert "sl-new" in cancel_calls or "sl-old" in cancel_calls or "tp-1" in cancel_calls

    await queue.stop()
    await processor
