"""Regression: position-update partial-close should be reconciled to a TP fill.

If a TP fires on the exchange but the WS order topic does not deliver the
fill event (network blip, stale-tick guard prior to the fix, etc.) and only
a position-update arrives showing the smaller remaining size, the previous
implementation just persisted the new quantity and moved on — the SL never
moved. The reconciler closes that gap by inferring the TP advancement after
a configurable delay if no order-topic event arrived in the meantime.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
from app.services.ws.manager import WebSocketManager  # noqa: E402

SYMBOL = "BTC/USDT:USDT"


def _build_position(*, original_qty: float = 1.0) -> PositionContext:
    entry = 100_000.0
    return PositionContext(
        position_id="pos-recon",
        account_id="acc-1",
        symbol=SYMBOL,
        side=PositionSide.LONG,
        state=PositionState.OPEN,
        entry_price=entry,
        original_quantity=original_qty,
        current_quantity=original_qty,
        current_sl_price=98_000.0,
        sl_exchange_order_id="sl-1",
        tp_mode="multi",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=50.0,
                trigger_price=entry * 1.01,
                status="open",
                exchange_order_id="tp-1-coid",
                sl_lock_pct=0.0,
            ),
            TPLevel(
                level=2,
                price_offset_pct=3.0,
                close_pct=50.0,
                trigger_price=entry * 1.03,
                status="open",
                exchange_order_id="tp-2-coid",
            ),
        ],
    )


def _make_manager(reconcile_delay: float = 0.05) -> tuple[WebSocketManager, AsyncMock]:
    queue = AsyncMock(spec=OrderExecutionQueue)

    async def _resolver(_position: PositionContext) -> OrderExecutionQueue:
        return queue

    async def _persist(_position: PositionContext) -> None:
        return None

    manager = WebSocketManager(
        adapter=AsyncMock(spec=ExchangeAdapter),
        account_id="acc-1",
        persist_position=_persist,
        order_queue_resolver=_resolver,
    )
    manager._warmed_up = True
    # Speed up the test with a short reconcile delay.
    manager.PARTIAL_CLOSE_RECONCILE_DELAY_SECONDS = reconcile_delay  # type: ignore[misc]
    return manager, queue


async def test_position_update_with_matching_delta_triggers_inferred_tp_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, queue = _make_manager(reconcile_delay=0.05)
    position = _build_position()
    manager.track_position(position)

    captured_events: list[str] = []

    async def _hook(event_type: str, _payload: dict[str, Any]) -> None:
        captured_events.append(event_type)

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        # Position shrinks to 0.5 — exactly TP1's close fraction.
        await manager._handle_position_update(
            {
                "type": "position",
                "symbol": SYMBOL,
                "size": 0.5,
            }
        )
        # Allow the deferred reconciler to fire.
        import asyncio

        await asyncio.sleep(0.15)
    finally:
        auto_trade_audit.set_audit_hook(None)

    assert position.tp_levels[0].status == "triggered"
    actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" in actions
    assert "multi_tp_inferred_from_position_update" in captured_events


async def test_position_update_with_unmatching_delta_does_not_advance() -> None:
    manager, queue = _make_manager(reconcile_delay=0.05)
    position = _build_position()
    manager.track_position(position)

    # Reduction by ~30% of original_qty doesn't match either TP level (50/50).
    await manager._handle_position_update(
        {
            "type": "position",
            "symbol": SYMBOL,
            "size": 0.7,
        }
    )
    import asyncio

    await asyncio.sleep(0.15)

    assert position.tp_levels[0].status == "open"
    assert position.tp_levels[1].status == "open"
    actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in actions
