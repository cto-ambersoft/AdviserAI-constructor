"""Regression: stale-tick guard must not silently drop TP/SL fill events.

Before the fix, ``_check_stale_tick`` was applied to every order-update
event. When a TP fill arrived with its trigger price (which can be several
percent away from the last seen mid-price), the guard rejected the event and
the multi-TP SL adjustment never ran.

The fix is in ``_handle_order_update``: stale-tick rejection is now ignored
for events that are fills or conditional-order events (``order_type``
∈ {stop_loss, take_profit, trailing_stop} or fill-bearing).
"""

from __future__ import annotations

import sys
from pathlib import Path
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
from app.services.ws.manager import WebSocketManager  # noqa: E402

SYMBOL = "BTC/USDT:USDT"


def _make_manager() -> tuple[WebSocketManager, AsyncMock]:
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
    return manager, queue


def _build_position() -> PositionContext:
    entry = 100_000.0
    return PositionContext(
        position_id="pos-stale",
        account_id="acc-1",
        symbol=SYMBOL,
        side=PositionSide.LONG,
        state=PositionState.OPEN,
        entry_price=entry,
        original_quantity=1.0,
        current_quantity=1.0,
        current_sl_price=99_000.0,
        sl_exchange_order_id="sl-1",
        tp_mode="multi",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=3.0,
                close_pct=50.0,
                trigger_price=entry * 1.03,
                status="open",
                exchange_order_id="tp-1-coid",
                sl_lock_pct=0.0,
            ),
            TPLevel(
                level=2,
                price_offset_pct=5.0,
                close_pct=50.0,
                trigger_price=entry * 1.05,
                status="open",
                exchange_order_id="tp-2-coid",
            ),
        ],
    )


async def test_tp_fill_event_is_routed_even_when_price_3pct_off_seed() -> None:
    """BTC/ETH stale threshold is 2%. A TP fill 3% above seed must still route."""
    manager, queue = _make_manager()
    position = _build_position()
    manager.track_position(position)

    # Seed last_good_prices with the entry price; the TP fill price is 3% above.
    normalized = WebSocketManager._normalize_symbol_key(SYMBOL)
    manager._last_prices[normalized] = position.entry_price
    manager._last_good_prices[normalized] = position.entry_price

    event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-1-coid",
        "client_order_id": "",
        "status": "filled",
        "order_type": "take_profit",
        "price": position.tp_levels[0].trigger_price,  # +3% off seed
        "filled_quantity": 0.5,
    }

    await manager._handle_order_update(event)

    actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" in actions


async def test_non_fill_price_event_still_subject_to_stale_tick_guard() -> None:
    """A non-fill order event with anomalous price is still rejected."""
    manager, queue = _make_manager()
    position = _build_position()
    manager.track_position(position)

    normalized = WebSocketManager._normalize_symbol_key(SYMBOL)
    manager._last_prices[normalized] = position.entry_price
    manager._last_good_prices[normalized] = position.entry_price

    event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "some-order",
        "client_order_id": "",
        "status": "new",
        "order_type": "limit",
        "price": position.entry_price * 1.20,  # 20% off — clearly stale
    }

    await manager._handle_order_update(event)

    # The non-fill event with no fill-bearing fields should be dropped before
    # routing; no replace_sl gets enqueued.
    actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in actions
