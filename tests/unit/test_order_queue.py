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


async def test_enqueue_replace_sl_with_same_target_coalesces_to_latest_params() -> None:
    """replace_sl with identical target price coalesces (latest params win)."""
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
                "new_trigger_price": 98000.0,  # SAME target → coalesce
                "new_quantity": 0.4,
                "client_order_id": "replace-sl-2",
            },
        )
    )

    assert queue._queue.qsize() == 1

    task = await asyncio.wait_for(queue._queue.get(), timeout=1.0)
    queue._queue.task_done()

    assert task.params["new_trigger_price"] == pytest.approx(98000.0)
    assert task.params["new_quantity"] == pytest.approx(0.4)
    assert task.params["client_order_id"] == "replace-sl-2"
    # The coalesce path bumps created_at so the latest-intent replacement
    # is processed at the freshest priority.
    assert task.created_at == pytest.approx(2.0)


async def test_enqueue_replace_sl_with_different_target_does_not_coalesce() -> None:
    """Two replace_sl tasks with different target prices both reach the queue.

    SL is safety-critical — silently merging a "move SL to 98000" with a "move SL
    to 98500" would lose the latter intent and leave the position at the wrong
    SL. Different targets must therefore be treated as distinct tasks.
    """
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
                "new_trigger_price": 98500.0,  # DIFFERENT target → 2 tasks
                "new_quantity": 0.5,
                "client_order_id": "replace-sl-2",
            },
        )
    )

    assert queue._queue.qsize() == 2


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


async def test_purge_pending_removes_matching_actions_and_tombstones_them() -> None:
    """``purge_pending`` drops in-flight protective tasks for a position.

    Used when a position transitions to CLOSED so the WS-manager-side
    cancel cleanup does not race with a queued ``replace_sl`` /
    ``place_sl`` / ``place_tp``. After purge, the dispatcher must not
    invoke the adapter for the dropped tasks.
    """
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-purge")

    base_params: dict[str, Any] = {
        "symbol": "BTC/USDT:USDT",
        "side": OrderSide.SELL,
        "client_order_id": "any",
    }
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=1.0,
            position_id="pos-purge",
            action="place_sl",
            params={
                **base_params,
                "quantity": 0.1,
                "trigger_price": 98000.0,
                "client_order_id": "sl-1",
            },
        )
    )
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=2.0,
            position_id="pos-purge",
            action="replace_sl",
            params={
                "symbol": "BTC/USDT:USDT",
                "existing_order_id": "old-sl",
                "new_trigger_price": 98500.0,
                "new_quantity": 0.1,
                "client_order_id": "rsl-1",
            },
        )
    )
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.NEW_CONDITIONAL,
            created_at=3.0,
            position_id="pos-purge",
            action="place_tp",
            params={
                **base_params,
                "level": 2,
                "quantity": 0.1,
                "trigger_price": 105000.0,
                "client_order_id": "tp-1",
            },
        )
    )
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.CANCEL_ORDER,
            created_at=4.0,
            position_id="pos-purge",
            action="cancel_order",
            params={"symbol": "BTC/USDT:USDT", "order_id": "old-1"},
        )
    )
    # Sibling position must not be affected.
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=5.0,
            position_id="pos-other",
            action="place_sl",
            params={
                **base_params,
                "quantity": 0.2,
                "trigger_price": 97000.0,
                "client_order_id": "sl-other",
            },
        )
    )

    purged_keys = await queue.purge_pending(
        "pos-purge", {"place_sl", "replace_sl", "place_tp", "replace_tp"}
    )

    # Three of the four pos-purge tasks (place_sl, replace_sl, place_tp)
    # should be tombstoned; cancel_order remains; pos-other untouched.
    assert len(purged_keys) == 3
    remaining = list(queue._pending_tasks.keys())
    assert any(key.endswith(":cancel_order:order:old-1") for key in remaining)
    assert any(key.startswith("pos-other:") for key in remaining)
    assert not any(key.startswith("pos-purge:place_sl") for key in remaining)
    assert not any(key.startswith("pos-purge:replace_sl") for key in remaining)
    assert not any(key.startswith("pos-purge:place_tp") for key in remaining)

    # Drain the queue and confirm the dispatcher skipped tombstoned tasks.
    processor = asyncio.create_task(queue.start_processing())
    await asyncio.wait_for(queue._queue.join(), timeout=1.0)
    await queue.stop()
    await asyncio.wait_for(processor, timeout=1.0)

    # Only the cancel_order and pos-other place_sl should have run.
    assert adapter.place_stop_loss.await_count == 1
    assert adapter.replace_sl.await_count == 0 if hasattr(adapter, "replace_sl") else True
    assert adapter.place_take_profit.await_count == 0
    adapter.cancel_conditional_order.assert_awaited_once()


async def test_resolve_emergency_quantity_returns_zero_when_no_positive_value() -> None:
    """``_resolve_emergency_quantity`` must return 0.0 (not raise) when
    none of the conventional keys carry a positive quantity.

    Defensive: the canonical SL path no longer enqueues emergency closes
    with qty=0 (multi_tp.py skips replace_sl when current_quantity<=0),
    but if any other caller does, we must not crash the queue worker.
    """
    assert OrderExecutionQueue._resolve_emergency_quantity({}) == 0.0
    assert OrderExecutionQueue._resolve_emergency_quantity(
        {"full_quantity": 0, "quantity": 0.0, "new_quantity": 0}
    ) == 0.0
    # First positive value still wins.
    assert OrderExecutionQueue._resolve_emergency_quantity(
        {"full_quantity": 0, "quantity": 0.42, "new_quantity": 1.0}
    ) == pytest.approx(0.42)


async def test_emergency_market_close_skipped_when_quantity_resolves_zero() -> None:
    """The dispatcher skips an emergency_market_close task built with qty=0
    rather than calling the adapter (which would either reject or close
    nothing). Mirrors the audit-side guard added with the qty<=0 fix.
    """
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-emerg-zero")

    processor = asyncio.create_task(queue.start_processing())
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.EMERGENCY_CLOSE,
            created_at=1.0,
            position_id="pos-zero",
            action="emergency_market_close",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "full_quantity": 0.0,
                "client_order_id": "emerg-zero-1",
            },
        )
    )
    await asyncio.wait_for(queue._queue.join(), timeout=1.0)
    await queue.stop()
    await asyncio.wait_for(processor, timeout=1.0)

    adapter.partial_close.assert_not_awaited()


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
