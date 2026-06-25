"""Regression: dispatched_sl_levels guards against re-entrant TP dispatch.

Production incident (Binance USDT-M): a single TP1 fill produced four pairs
of ``sl_adjustment_decided`` + ``sl_adjustment_dispatched`` audits in the
same second, then three fatal ``replace_sl`` errors plus a fatal
``emergency_market_close`` (-2022). The pre-existing
``level.status == "triggered"`` guard checks status AFTER the work begins;
multiple near-simultaneous calls within the same per-position lock window
could all observe ``status == "open"`` before any of them wrote
``triggered`` and dispatch in parallel.

These tests pin the second guard: ``position.dispatched_sl_levels`` is
populated BEFORE any await, so a re-entry on the same ``triggered_level``
short-circuits at the top.
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

from app.services.exchange.adapter import ExchangeAdapter  # noqa: E402
from app.services.position.context import (  # noqa: E402
    PositionContext,
    PositionSide,
    TPLevel,
)
from app.services.position.order_queue import OrderExecutionQueue  # noqa: E402
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.sl_tp.multi_tp import MultiTPEngine  # noqa: E402


def _build_position() -> PositionContext:
    return PositionContext(
        position_id="pos-dedup-1",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        state=PositionState.OPEN,
        entry_price=100_000.0,
        original_quantity=1.0,
        current_quantity=1.0,
        current_sl_price=98_000.0,
        sl_exchange_order_id="sl-1",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=33.0,
                trigger_price=101_000.0,
                status="open",
                exchange_order_id="tp-1",
                sl_lock_pct=0.0,
            ),
            TPLevel(
                level=2,
                price_offset_pct=2.0,
                close_pct=33.0,
                trigger_price=102_000.0,
                status="open",
                exchange_order_id="tp-2",
                sl_lock_pct=50.0,
            ),
            TPLevel(
                level=3,
                price_offset_pct=3.0,
                close_pct=34.0,
                trigger_price=103_000.0,
                status="open",
                exchange_order_id="tp-3",
            ),
        ],
    )


async def test_dispatched_sl_levels_set_marks_level_after_first_call() -> None:
    position = _build_position()
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, queue)

    await engine.handle_tp_triggered(triggered_level=0)

    assert 0 in position.dispatched_sl_levels
    # One replace_sl from this single legitimate fill.
    assert queue.enqueue.await_count == 1


async def test_duplicate_handle_tp_triggered_short_circuits_via_dedup_set() -> None:
    """Second call for the same level must not double-dispatch even after the
    first call's ``level.status`` write happens to revert (defence-in-depth
    against any future code path that resets status)."""
    position = _build_position()
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, queue)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        await engine.handle_tp_triggered(triggered_level=0)
        # Simulate any path that wipes status while leaving the dispatch
        # set populated (e.g. a hydration that overwrote tp_levels).
        position.tp_levels[0].status = "open"
        await engine.handle_tp_triggered(triggered_level=0)
    finally:
        auto_trade_audit.set_audit_hook(None)

    # Only ONE replace_sl despite the second invocation.
    assert queue.enqueue.await_count == 1

    # Second call emitted a duplicate-dispatch audit with the new reason.
    duplicate_events = [
        event for event in audits if event[0] == "multi_tp_duplicate_dispatch_ignored"
    ]
    assert len(duplicate_events) == 1
    assert duplicate_events[0][1]["reason"] == "already_dispatched"


async def test_concurrent_dispatch_for_same_level_yields_single_replace_sl() -> None:
    """4 concurrent ``handle_tp_triggered(0)`` invocations: ONE replace_sl.

    Mirrors the production incident's audit shape — four near-simultaneous
    calls within the same lock window for one real TP1 fill.
    """
    position = _build_position()
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, queue)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        await asyncio.gather(*[engine.handle_tp_triggered(triggered_level=0) for _ in range(4)])
    finally:
        auto_trade_audit.set_audit_hook(None)

    assert queue.enqueue.await_count == 1
    decided = [
        event for event in audits if event[0] == "sl_adjustment_decided"
    ]
    assert len(decided) == 1
    duplicates = [
        event for event in audits if event[0] == "multi_tp_duplicate_dispatch_ignored"
    ]
    # Three duplicates (one for each of the redundant 4 calls).
    assert len(duplicates) == 3
