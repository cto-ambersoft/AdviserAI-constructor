"""Regression: ``_handle_position_update`` acquires the per-position lock.

Without the lock, ``_handle_position_update`` mutates
``position.current_quantity`` in parallel with the engine path
(``_route_to_position`` → ``MultiTPEngine.handle_tp_triggered``) which
also mutates ``current_quantity``. The production audit trail showed
four ``sl_adjustment_decided`` pairs in the same millisecond for a
single TP1 fill, consistent with concurrent unlocked writes.

This test asserts that ``_handle_position_update`` serialises against
``_route_to_position`` on the same position id.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
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
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.ws.manager import WebSocketManager  # noqa: E402

SYMBOL = "BTC/USDT:USDT"


def _build_position() -> PositionContext:
    return PositionContext(
        position_id="pos-lock-1",
        symbol=SYMBOL,
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
        ],
    )


async def test_position_update_serialises_with_route_to_position() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    manager = WebSocketManager(adapter=adapter, account_id="acc-1")
    position = _build_position()
    manager.track_position(position)

    # Acquire the per-position lock manually and start ``_handle_position_update``.
    # It must wait until we release the lock — proving it tries to acquire.
    lock = manager._get_position_lock(position)  # type: ignore[attr-defined]
    await lock.acquire()

    started = asyncio.Event()

    async def _call_handler() -> None:
        started.set()
        await manager._handle_position_update(  # type: ignore[attr-defined]
            {
                "type": "position",
                "symbol": SYMBOL,
                "size": 0.5,
            }
        )

    handler_task = asyncio.create_task(_call_handler())
    await started.wait()
    await asyncio.sleep(0.05)

    # While we hold the lock, the handler must NOT have made progress.
    # Position quantity should still be 1.0 (handler hasn't entered the body).
    assert position.current_quantity == 1.0
    assert not handler_task.done()

    # Release and let the handler run.
    lock.release()
    await asyncio.wait_for(handler_task, timeout=1.0)

    # Now the handler has applied its mutation under the lock.
    assert position.current_quantity == pytest.approx(0.5)
