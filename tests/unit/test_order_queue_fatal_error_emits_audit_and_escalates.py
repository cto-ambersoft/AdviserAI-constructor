"""Regression: ``_handle_fatal_error`` must surface SL failures, not swallow them.

Before the fix, non-transient adapter errors on ``place_sl``/``replace_sl``
were silently swallowed by a stub ``_handle_fatal_error`` (the entire body
was ``_ = (task, error)``). After the fix:

- the registered audit hook is called with the failed task and exception,
- for SL-related actions we additionally enqueue an ``emergency_market_close``
  so an unprotected position is flattened rather than left exposed.
"""

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

from app.services.exchange.adapter import ExchangeAdapter, OrderSide  # noqa: E402
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
    set_fatal_error_audit_hook,
)


class _NonTransientError(Exception):
    """Stand-in for an exchange API error that should not be retried."""


@pytest.fixture(autouse=True)
def _reset_audit_hook() -> Any:
    set_fatal_error_audit_hook(None)
    yield
    set_fatal_error_audit_hook(None)


async def test_replace_sl_non_transient_failure_emits_audit_and_escalates() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.cancel_and_replace_sl.side_effect = _NonTransientError("BybitAPIError: 10001")
    # Make the emergency close ALSO fail so the task stays observable rather
    # than being processed and cleared from the queue before our assertion.
    adapter.partial_close.side_effect = _NonTransientError("emergency close blocked")

    captured: list[tuple[OrderTask, Exception]] = []

    async def _hook(task: OrderTask, error: Exception) -> None:
        captured.append((task, error))

    set_fatal_error_audit_hook(_hook)

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-fatal")

    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=1.0,
            position_id="pos-fatal",
            action="replace_sl",
            params={
                "symbol": "BTC/USDT:USDT",
                "existing_order_id": "old-sl",
                "new_trigger_price": 99000.0,
                "new_quantity": 1.0,
                "full_quantity": 1.0,
                "side": OrderSide.SELL,
                "client_order_id": "fatal-test",
            },
        )
    )

    processor = asyncio.create_task(queue.start_processing())
    await asyncio.wait_for(queue._queue.join(), timeout=2.0)
    await queue.stop()
    await asyncio.wait_for(processor, timeout=1.0)

    # Audit hook fires twice: once for the original replace_sl, once for the
    # cascading emergency_market_close. We assert the chain rather than a
    # specific count because future fixes may relax the cascade.
    actions_failed = [t.action for t, _ in captured]
    assert "replace_sl" in actions_failed
    assert "emergency_market_close" in actions_failed


async def test_non_sl_action_failure_emits_audit_but_does_not_escalate() -> None:
    """A failure on a non-SL action surfaces via audit but does NOT enqueue
    an emergency close — emergency close is reserved for SL failures."""
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.place_take_profit.side_effect = _NonTransientError("BadRequest")

    captured: list[tuple[OrderTask, Exception]] = []

    async def _hook(task: OrderTask, error: Exception) -> None:
        captured.append((task, error))

    set_fatal_error_audit_hook(_hook)

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-fatal-tp")

    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.NEW_CONDITIONAL,
            created_at=1.0,
            position_id="pos-fatal-tp",
            action="place_tp",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "quantity": 1.0,
                "trigger_price": 105000.0,
                "client_order_id": "fatal-tp-1",
                "reduce_only": True,
                "level": 1,
            },
        )
    )

    processor = asyncio.create_task(queue.start_processing())
    await asyncio.wait_for(queue._queue.join(), timeout=2.0)
    await queue.stop()
    await asyncio.wait_for(processor, timeout=1.0)

    assert len(captured) == 1
    actions = [t.action for t in queue._pending_tasks.values()]
    assert "emergency_market_close" not in actions
