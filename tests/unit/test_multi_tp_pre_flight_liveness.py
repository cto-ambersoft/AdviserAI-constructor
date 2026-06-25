"""Regression: pre-flight liveness check in MultiTPEngine.

If the live exchange position is flat (closePosition=true SL auto-fired
between dispatch and event delivery), the engine MUST NOT enqueue a
``replace_sl`` — otherwise the queue attempts a STOP placement on a
gone position and the resulting -2022 cascades.

Engine no longer enforces a mark-vs-trigger clamp/skip; that
responsibility was delegated to the adapter-level classifier so the
engine doesn't false-positive in scenarios where the harness/mock mark
price is stale relative to the configured TP triggers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import (  # noqa: E402
    ExchangeAdapter,
    PositionSnapshot,
)
from app.services.exchange.adapter import (  # noqa: E402
    PositionSide as AdapterPositionSide,
)
from app.services.position.context import (  # noqa: E402
    PositionContext,
    PositionSide,
    TPLevel,
)
from app.services.position.order_queue import OrderExecutionQueue  # noqa: E402
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.sl_tp.multi_tp import MultiTPEngine  # noqa: E402


def _snapshot(*, size: float, mark: float) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="BTC/USDT:USDT",
        side=AdapterPositionSide.LONG,
        size=size,
        entry_price=100_000.0,
        unrealized_pnl=0.0,
        leverage=10,
        mark_price=mark,
        liquidation_price=90_000.0,
        open_orders=[],
    )


def _build_position(
    *,
    side: PositionSide = PositionSide.LONG,
    sl_lock_pct: float | None = 50.0,
) -> PositionContext:
    entry = 100_000.0
    tp1_price = entry * 1.01 if side == PositionSide.LONG else entry * 0.99
    return PositionContext(
        position_id="pos-pflight",
        symbol="BTC/USDT:USDT",
        side=side,
        state=PositionState.OPEN,
        entry_price=entry,
        original_quantity=1.0,
        current_quantity=1.0,
        current_sl_price=entry * 0.98 if side == PositionSide.LONG else entry * 1.02,
        sl_exchange_order_id="sl-1",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=33.0,
                trigger_price=tp1_price,
                status="open",
                exchange_order_id="tp-1",
                sl_lock_pct=sl_lock_pct,
            ),
            TPLevel(
                level=2,
                price_offset_pct=2.0,
                close_pct=67.0,
                trigger_price=(
                    entry * 1.02 if side == PositionSide.LONG else entry * 0.98
                ),
                status="open",
                exchange_order_id="tp-2",
            ),
        ],
    )


async def test_position_flat_skips_replace_sl_with_audit() -> None:
    position = _build_position()
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.get_position.return_value = None  # exchange says: position is gone
    queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, queue)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        await engine.handle_tp_triggered(triggered_level=0)
    finally:
        auto_trade_audit.set_audit_hook(None)

    queue.enqueue.assert_not_awaited()
    skipped = [
        event for event in audits if event[0] == "sl_adjustment_skipped_position_already_flat"
    ]
    assert len(skipped) == 1


async def test_position_flat_via_dust_size_also_skips() -> None:
    position = _build_position()
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.get_position.return_value = _snapshot(size=1e-12, mark=100_500.0)
    queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, queue)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        await engine.handle_tp_triggered(triggered_level=0)
    finally:
        auto_trade_audit.set_audit_hook(None)

    queue.enqueue.assert_not_awaited()
    skipped = [
        event for event in audits if event[0] == "sl_adjustment_skipped_position_already_flat"
    ]
    assert len(skipped) == 1


async def test_live_position_present_enqueues_replace_sl() -> None:
    """Sanity: when get_position returns a healthy snapshot, replace_sl flows."""
    position = _build_position(sl_lock_pct=50.0)
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.get_position.return_value = _snapshot(size=0.5, mark=101_500.0)
    queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, queue)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        await engine.handle_tp_triggered(triggered_level=0)
    finally:
        auto_trade_audit.set_audit_hook(None)

    decided = [
        event for event in audits if event[0] == "sl_adjustment_decided"
    ]
    assert len(decided) == 1
    queue.enqueue.assert_awaited_once()


async def test_mock_adapter_returning_non_snapshot_does_not_skip() -> None:
    """A test adapter that returns ``object()`` (unknown shape) must NOT
    block the dispatch — the engine treats it as 'unknown' and proceeds.
    This preserves the existing test surface in test_multi_tp.py."""
    position = _build_position()
    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.get_position.return_value = object()
    queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, queue)

    await engine.handle_tp_triggered(triggered_level=0)
    queue.enqueue.assert_awaited_once()
