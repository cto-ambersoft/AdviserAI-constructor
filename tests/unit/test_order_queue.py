"""Unit tests for priority order execution queue."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import (  # noqa: E402
    ExchangeAdapter,
    OrderSide,
    PartialCloseResult,  # noqa: E402
    RateLimitState,
)
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
    TransientExchangeError,
)


def _rate_state(retry_after: float | None = None) -> RateLimitState:
    return RateLimitState(
        order_count_10s=0,
        order_count_1m=0,
        order_limit_10s=300,
        order_limit_1m=1200,
        weight_used_1m=0,
        weight_limit_1m=2400,
        retry_after=retry_after,
    )


async def test_priority_ordering_executes_sl_before_tp() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True

    execution_order: list[str] = []

    async def _record_sl(**_: Any) -> object:
        execution_order.append("sl")
        return object()

    async def _record_tp(**_: Any) -> object:
        execution_order.append("tp")
        return object()

    adapter.place_stop_loss.side_effect = _record_sl
    adapter.place_take_profit.side_effect = _record_tp

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
    processor = asyncio.create_task(queue.start_processing())

    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.TP_ADJUSTMENT,
            created_at=20.0,
            position_id="pos-1",
            action="place_tp",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "quantity": 0.2,
                "trigger_price": 103000.0,
                "client_order_id": "tp-1",
            },
        )
    )
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=10.0,
            position_id="pos-1",
            action="place_sl",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "quantity": 0.2,
                "trigger_price": 98000.0,
                "client_order_id": "sl-1",
            },
        )
    )

    await asyncio.wait_for(queue._queue.join(), timeout=1.0)
    await queue.stop()
    await asyncio.wait_for(processor, timeout=1.0)

    assert execution_order == ["sl", "tp"]


async def test_emergency_priority_bypasses_all_pending_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-2")
    executed: list[str] = []

    async def _record_task(task: OrderTask) -> None:
        executed.append(task.action)
        await queue.stop()

    monkeypatch.setattr(queue, "_execute_task", _record_task)

    processor = asyncio.create_task(queue.start_processing())

    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.TP_ADJUSTMENT,
            created_at=1.0,
            position_id="pos-2",
            action="place_tp",
            params={},
        )
    )
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.NEW_CONDITIONAL,
            created_at=2.0,
            position_id="pos-2",
            action="place_entry",
            params={},
        )
    )
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=3.0,
            position_id="pos-2",
            action="replace_sl",
            params={},
        )
    )
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.EMERGENCY_SL,
            created_at=4.0,
            position_id="pos-2",
            action="place_sl",
            params={},
        )
    )

    await asyncio.wait_for(processor, timeout=1.0)
    assert executed[0] == "place_sl"


async def test_enqueue_deduplicates_same_position_action_and_keeps_latest_params() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-3")

    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=1.0,
            position_id="position_1",
            action="replace_sl",
            params={
                "symbol": "BTC/USDT:USDT",
                "existing_order_id": "old-sl",
                "new_trigger_price": 98000.0,
                "new_quantity": 0.5,
                "client_order_id": "replace-sl-1",
            },
        )
    )
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=2.0,
            position_id="position_1",
            action="replace_sl",
            params={
                "symbol": "BTC/USDT:USDT",
                "existing_order_id": "old-sl",
                "new_trigger_price": 98500.0,
                "new_quantity": 0.5,
                "client_order_id": "replace-sl-2",
            },
        )
    )

    assert queue._queue.qsize() == 1

    task = await asyncio.wait_for(queue._queue.get(), timeout=1.0)
    queue._queue.task_done()

    assert task.params["new_trigger_price"] == pytest.approx(98500.0)
    assert task.params["client_order_id"] == "replace-sl-2"


async def test_transient_error_retries_and_reenqueues_task(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.place_stop_loss.side_effect = TransientExchangeError("temporary exchange issue")

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-4")
    task = OrderTask(
        priority=OrderPriority.SL_ADJUSTMENT,
        created_at=1.0,
        position_id="pos-retry",
        action="place_sl",
        params={
            "symbol": "BTC/USDT:USDT",
            "side": OrderSide.SELL,
            "quantity": 1.0,
            "trigger_price": 97000.0,
            "client_order_id": "sl-retry-1",
        },
    )

    sleep_calls: list[float] = []

    async def _sleep_and_stop(delay: float) -> None:
        sleep_calls.append(delay)
        queue._processing = False

    monkeypatch.setattr("app.services.position.order_queue.asyncio.sleep", _sleep_and_stop)

    processor = asyncio.create_task(queue.start_processing())
    await queue.enqueue(task)
    await asyncio.wait_for(processor, timeout=1.0)

    assert task.retry_count == 1
    assert queue._queue.qsize() == 1
    assert sleep_calls


async def test_sl_max_retries_escalates_to_emergency_market_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.place_stop_loss.side_effect = TransientExchangeError("sl placement failed")

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-5")

    async def _emergency_close(**_: Any) -> PartialCloseResult:
        await queue.stop()
        return PartialCloseResult(
            executed_qty=1.0,
            avg_price=96500.0,
            remaining_qty=0.0,
            order_id="emergency-close-1",
            commission=0.0,
        )

    adapter.partial_close.side_effect = _emergency_close

    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.position.order_queue.asyncio.sleep", sleep_mock)

    processor = asyncio.create_task(queue.start_processing())
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=1.0,
            position_id="pos-escalate",
            action="place_sl",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "quantity": 1.0,
                "trigger_price": 96000.0,
                "client_order_id": "sl-escalate-1",
            },
            max_retries=3,
        )
    )

    await asyncio.wait_for(processor, timeout=1.0)

    assert adapter.place_stop_loss.await_count == 4
    adapter.partial_close.assert_awaited_once()
    emergency_kwargs = adapter.partial_close.call_args.kwargs
    assert emergency_kwargs["order_type"] == "market"
    assert emergency_kwargs["quantity"] == pytest.approx(1.0)


async def test_rate_limit_pause_waits_before_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.side_effect = [False, True]
    adapter.get_rate_limit_state.return_value = _rate_state(retry_after=0.25)

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-6")

    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.position.order_queue.asyncio.sleep", sleep_mock)

    processor = asyncio.create_task(queue.start_processing())
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.TP_ADJUSTMENT,
            created_at=1.0,
            position_id="pos-rate-limit",
            action="place_tp",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "quantity": 0.3,
                "trigger_price": 104000.0,
                "client_order_id": "tp-rate-1",
            },
        )
    )

    await asyncio.wait_for(queue._queue.join(), timeout=1.0)
    await queue.stop()
    await asyncio.wait_for(processor, timeout=1.0)

    adapter.place_take_profit.assert_awaited_once()
    sleep_mock.assert_awaited()
    first_wait = sleep_mock.await_args_list[0].args[0]
    assert first_wait == pytest.approx(0.25)
