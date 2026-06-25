"""Regression: ``OrderExecutionQueue.await_quiescent`` blocks until in-flight
tasks for the position finish.

``WebSocketManager._cancel_remaining_orders`` uses this to avoid racing
``cancel_and_replace_sl`` (place-first / cancel-last on Binance). Without
the wait the on-exchange DELETE deletes the SL the queue task just minted.
"""

from __future__ import annotations

import asyncio
import sys
import time
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
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
)


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


def _replace_sl_task() -> OrderTask:
    return OrderTask(
        priority=OrderPriority.SL_ADJUSTMENT,
        created_at=time.time(),
        position_id="pos-quiesce",
        action="replace_sl",
        params={
            "symbol": "BTC/USDT:USDT",
            "side": OrderSide.SELL,
            "existing_order_id": "sl-1",
            "new_trigger_price": 100_500.0,
            "trigger_price": 100_500.0,
            "new_quantity": 1.0,
            "full_quantity": 1.0,
            "client_order_id": "coid-1",
            "close_position": True,
            "reason": "tp1_hit_sl_adjustment",
        },
    )


async def test_await_quiescent_blocks_until_in_flight_task_completes() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.get_rate_limit_state.return_value = _rate_state()

    release = asyncio.Event()

    async def _slow_replace(**_: Any) -> ConditionalOrderResult:
        # Block until the test releases us so ``await_quiescent`` has
        # something to wait on.
        await release.wait()
        return ConditionalOrderResult(
            exchange_order_id="new-sl",
            client_order_id="coid-1",
            order_type="stop_loss",
            trigger_price=100_500.0,
            quantity=1.0,
            status="new",
        )

    adapter.cancel_and_replace_sl.side_effect = _slow_replace

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
    processor = asyncio.create_task(queue.start_processing())
    try:
        await queue.enqueue(_replace_sl_task())
        # Give the processor a moment to dequeue and start executing.
        await asyncio.sleep(0.05)

        # Start the quiescent wait — it must NOT return while the task is
        # still in ``_slow_replace``.
        quiesce_task = asyncio.create_task(
            queue.await_quiescent("pos-quiesce", {"replace_sl"}, timeout=2.0)
        )
        await asyncio.sleep(0.05)
        assert not quiesce_task.done()

        # Release the in-flight task and confirm await_quiescent returns True.
        release.set()
        result = await asyncio.wait_for(quiesce_task, timeout=1.0)
        assert result is True
    finally:
        await queue.stop()
        await processor


async def test_await_quiescent_times_out_when_task_hangs() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.get_rate_limit_state.return_value = _rate_state()

    forever = asyncio.Event()

    async def _hangs(**_: Any) -> ConditionalOrderResult:
        await forever.wait()  # never set
        raise RuntimeError("unreachable")

    adapter.cancel_and_replace_sl.side_effect = _hangs

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
    processor = asyncio.create_task(queue.start_processing())
    try:
        await queue.enqueue(_replace_sl_task())
        await asyncio.sleep(0.05)
        result = await queue.await_quiescent("pos-quiesce", {"replace_sl"}, timeout=0.1)
        assert result is False
    finally:
        forever.set()  # unblock so the processor can exit
        await queue.stop()
        await processor


async def test_await_quiescent_returns_true_when_no_matching_tasks() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")

    result = await queue.await_quiescent("pos-nothing", {"replace_sl"}, timeout=0.1)
    assert result is True
