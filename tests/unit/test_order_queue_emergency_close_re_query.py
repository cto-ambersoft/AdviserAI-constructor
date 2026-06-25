"""Regression: emergency_market_close re-queries exchange before reducing.

Production incident: an SL replacement failed terminally and the queue
enqueued an ``emergency_market_close`` with ``full_quantity`` captured at
``replace_sl`` time. By the time the emergency close ran, Binance had
already auto-closed the position via a ``closePosition=true`` SL that
fired. ``partial_close(reduceOnly=true, qty=stale)`` then returned
``-2022 ReduceOnly Order is rejected``.

This test pins the new behaviour: before issuing the close, the queue
calls ``adapter.get_position(symbol)``. When the live position is gone
(``None`` or size <= dust), it emits ``emergency_close_skipped_position_flat``
and does NOT call ``partial_close``. When the live size is smaller than
the params-provided quantity, the close is clamped to the live size.
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

from app.services.exchange.adapter import (  # noqa: E402
    ExchangeAdapter,
    OrderSide,
    PartialCloseResult,
    PositionSide,
    PositionSnapshot,
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


def _snapshot(size: float) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        size=size,
        entry_price=100_000.0,
        unrealized_pnl=0.0,
        leverage=10,
        mark_price=101_000.0,
        liquidation_price=90_000.0,
        open_orders=[],
    )


async def _drive_one_task(queue: OrderExecutionQueue) -> None:
    processor = asyncio.create_task(queue.start_processing())
    try:
        await asyncio.sleep(0.1)
    finally:
        await queue.stop()
        await processor


async def test_emergency_close_skipped_when_get_position_returns_none() -> None:
    adapter = _build_adapter()
    adapter.get_position.return_value = None

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    set_safety_audit_hook(_hook)
    try:
        queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
        await queue.enqueue(
            OrderTask(
                priority=OrderPriority.EMERGENCY_CLOSE,
                created_at=1.0,
                position_id="pos-1",
                action="emergency_market_close",
                params={
                    "symbol": "BTC/USDT:USDT",
                    "side": OrderSide.SELL,
                    "full_quantity": 1.5,
                    "client_order_id": "emergency-1",
                    "reason": "test",
                },
            )
        )
        await _drive_one_task(queue)
    finally:
        set_safety_audit_hook(None)

    adapter.partial_close.assert_not_awaited()
    skipped = [event for event in audits if event[0] == "emergency_close_skipped_position_flat"]
    assert len(skipped) == 1
    assert skipped[0][1]["live_size"] is None


async def test_emergency_close_skipped_when_live_size_at_dust() -> None:
    adapter = _build_adapter()
    adapter.get_position.return_value = _snapshot(size=1e-9)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    set_safety_audit_hook(_hook)
    try:
        queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
        await queue.enqueue(
            OrderTask(
                priority=OrderPriority.EMERGENCY_CLOSE,
                created_at=1.0,
                position_id="pos-1",
                action="emergency_market_close",
                params={
                    "symbol": "BTC/USDT:USDT",
                    "side": OrderSide.SELL,
                    "full_quantity": 1.5,
                    "client_order_id": "emergency-2",
                    "reason": "test",
                },
            )
        )
        await _drive_one_task(queue)
    finally:
        set_safety_audit_hook(None)

    adapter.partial_close.assert_not_awaited()
    skipped = [event for event in audits if event[0] == "emergency_close_skipped_position_flat"]
    assert len(skipped) == 1


async def test_emergency_close_clamps_quantity_to_live_size_when_smaller() -> None:
    adapter = _build_adapter()
    adapter.get_position.return_value = _snapshot(size=0.4)  # live position smaller
    adapter.partial_close.return_value = PartialCloseResult(
        executed_qty=0.4,
        avg_price=100.0,
        remaining_qty=0.0,
        order_id="order-emerg",
        commission=0.0,
    )

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.EMERGENCY_CLOSE,
            created_at=1.0,
            position_id="pos-1",
            action="emergency_market_close",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "full_quantity": 1.5,  # stale, larger than live
                "client_order_id": "emergency-3",
                "reason": "test",
            },
        )
    )
    await _drive_one_task(queue)

    adapter.partial_close.assert_awaited_once()
    kwargs = adapter.partial_close.await_args.kwargs
    assert kwargs["quantity"] == pytest.approx(0.4)
    assert kwargs["order_type"] == "market"


async def test_emergency_close_uses_params_quantity_when_adapter_returns_unknown() -> None:
    """Backwards-compat: a mock adapter that returns a non-snapshot Mock
    must fall back to params-provided quantity instead of skipping."""
    adapter = _build_adapter()
    # Default Mock returns a MagicMock (no isinstance(PositionSnapshot)) —
    # the queue treats this as "unknown live state" and uses params.
    adapter.get_position.return_value = object()
    adapter.partial_close.return_value = PartialCloseResult(
        executed_qty=1.5,
        avg_price=100.0,
        remaining_qty=0.0,
        order_id="order-emerg",
        commission=0.0,
    )

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")
    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.EMERGENCY_CLOSE,
            created_at=1.0,
            position_id="pos-1",
            action="emergency_market_close",
            params={
                "symbol": "BTC/USDT:USDT",
                "side": OrderSide.SELL,
                "full_quantity": 1.5,
                "client_order_id": "emergency-4",
                "reason": "test",
            },
        )
    )
    await _drive_one_task(queue)

    adapter.partial_close.assert_awaited_once()
    kwargs = adapter.partial_close.await_args.kwargs
    assert kwargs["quantity"] == pytest.approx(1.5)
