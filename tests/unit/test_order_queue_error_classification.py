"""Regression: queue routes typed exchange errors to skip-vs-fatal correctly.

Before the fix, every adapter exception that wasn't ``TransientExchangeError``
went through ``_handle_fatal_error`` → ``emergency_market_close``. The
production cascade burned through this path for ``-2021`` / ``-2022`` which
are not safety failures but config / liveness signals. This test pins:

- ``PlacementWouldImmediatelyTriggerError`` → audit
  ``sl_adjustment_skipped_would_trigger_immediately_vs_mark``,
  NO emergency close.
- ``PositionAlreadyFlatError`` → audit
  ``sl_adjustment_skipped_position_already_flat``, NO emergency close.
- Plain ``Exception`` → existing fatal-error escalation path
  (``emergency_market_close`` enqueued).
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
    ExchangeAdapter,
    OrderSide,
    PlacementWouldImmediatelyTriggerError,
    PositionAlreadyFlatError,
    RateLimitState,
)
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
    set_safety_audit_hook,
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


def _build_adapter() -> AsyncMock:
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.get_rate_limit_state.return_value = _rate_state()
    return adapter


async def _drive(queue: OrderExecutionQueue) -> None:
    processor = asyncio.create_task(queue.start_processing())
    try:
        await asyncio.sleep(0.1)
    finally:
        await queue.stop()
        await processor


def _replace_sl_task(action: str = "replace_sl") -> OrderTask:
    return OrderTask(
        priority=OrderPriority.SL_ADJUSTMENT,
        created_at=1.0,
        position_id="pos-1",
        action=action,
        params={
            "symbol": "BTC/USDT:USDT",
            "side": OrderSide.SELL,
            "existing_order_id": "sl-1",
            "new_trigger_price": 101_000.0,
            "trigger_price": 101_000.0,
            "new_quantity": 1.0,
            "full_quantity": 1.0,
            "client_order_id": "replace-1",
            "close_position": True,
            "reason": "tp1_hit_sl_adjustment",
        },
    )


async def test_placement_would_immediately_trigger_skips_with_audit_no_emergency_close() -> None:
    adapter = _build_adapter()
    adapter.cancel_and_replace_sl.side_effect = PlacementWouldImmediatelyTriggerError(
        "code=-2021",
        code=-2021,
        payload={"code": -2021, "msg": "Order would immediately trigger."},
        requested_trigger=101_000.0,
        mark_price=100_950.0,
    )

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    set_safety_audit_hook(_hook)
    try:
        queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
        await queue.enqueue(_replace_sl_task())
        await _drive(queue)
    finally:
        set_safety_audit_hook(None)

    target_event = "sl_adjustment_skipped_would_trigger_immediately_vs_mark"
    skipped = [event for event in audits if event[0] == target_event]
    assert len(skipped) == 1
    assert skipped[0][1]["code"] == -2021
    assert skipped[0][1]["requested_trigger"] == 101_000.0
    # No emergency_market_close should have been enqueued.
    assert adapter.partial_close.await_count == 0


async def test_position_already_flat_skips_with_audit_no_emergency_close() -> None:
    adapter = _build_adapter()
    adapter.cancel_and_replace_sl.side_effect = PositionAlreadyFlatError(
        "position gone",
        code=-2022,
        symbol="BTC/USDT:USDT",
    )

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    set_safety_audit_hook(_hook)
    try:
        queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
        await queue.enqueue(_replace_sl_task())
        await _drive(queue)
    finally:
        set_safety_audit_hook(None)

    skipped = [
        event for event in audits if event[0] == "sl_adjustment_skipped_position_already_flat"
    ]
    assert len(skipped) == 1
    assert skipped[0][1]["code"] == -2022
    assert adapter.partial_close.await_count == 0


async def test_unclassified_exception_still_escalates_to_emergency_close() -> None:
    """Sanity: existing fatal-error path is not broken by the new branches."""
    adapter = _build_adapter()
    adapter.cancel_and_replace_sl.side_effect = RuntimeError("something else broke")

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
    await queue.enqueue(_replace_sl_task())
    await _drive(queue)

    # The cascading emergency_market_close task should have been enqueued
    # (and will then attempt partial_close — adapter mock returns Mock).
    # We assert by checking that partial_close was invoked at least once
    # OR that adapter.get_position was called (the new safety re-query in
    # the emergency close path).
    assert adapter.get_position.await_count >= 1
