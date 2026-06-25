"""Regression: ``WebSocketManager.track_position`` preserves in-memory state.

Hydration runs every 60 s and rebuilds a fresh ``PositionContext`` from
DB. The old behaviour replaced the live ctx with the snapshot, silently
rewinding engine mutations whenever persist had not caught up (the prod
service runs persist after each lifecycle event, so most of the time
this is fine — but the 60 s cadence makes the race window very wide).
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
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.ws.manager import WebSocketManager  # noqa: E402

SYMBOL = "BTC/USDT:USDT"


def _build_ctx(
    *,
    current_quantity: float,
    tp1_status: str,
    tp1_trigger: float,
    entry_price: float = 100_000.0,
) -> PositionContext:
    return PositionContext(
        position_id="pos-hydrate-1",
        symbol=SYMBOL,
        side=PositionSide.LONG,
        state=PositionState.OPEN,
        entry_price=entry_price,
        original_quantity=1.0,
        current_quantity=current_quantity,
        current_sl_price=entry_price * 0.98,
        sl_exchange_order_id="sl-live",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=33.0,
                trigger_price=tp1_trigger,
                status=tp1_status,
                exchange_order_id="tp-1-live",
                sl_lock_pct=50.0,
            ),
            TPLevel(
                level=2,
                price_offset_pct=2.0,
                close_pct=33.0,
                trigger_price=entry_price * 1.02,
                status="open",
                exchange_order_id="tp-2-live",
                sl_lock_pct=50.0,
            ),
        ],
    )


def _make_manager() -> WebSocketManager:
    adapter = AsyncMock(spec=ExchangeAdapter)
    return WebSocketManager(adapter=adapter, account_id="acc-1")


async def test_hydration_preserves_in_memory_mutations_on_live_ctx() -> None:
    manager = _make_manager()

    live = _build_ctx(current_quantity=1.0, tp1_status="open", tp1_trigger=101_000.0)
    manager.track_position(live)
    assert manager.is_tracked("pos-hydrate-1")

    # Simulate engine progress: TP1 fired, qty halved, dispatched set marked.
    live.current_quantity = 0.5
    live.tp_levels[0].status = "triggered"
    live.dispatched_sl_levels.add(0)
    live.sl_exchange_order_id = "sl-new-after-tp1"

    # Now hydration rebuilds a fresh ctx from DB. In production the DB row
    # might be slightly stale (persist hadn't caught up with the tp1 fill).
    db_snapshot = _build_ctx(
        current_quantity=1.0,  # stale DB qty
        tp1_status="open",  # stale DB status
        tp1_trigger=101_500.0,  # operator updated trigger in DB
    )
    db_snapshot.dispatched_sl_levels = set()  # fresh in-memory invariant on snapshot
    db_snapshot.sl_exchange_order_id = "sl-stale"
    # Without replace_in_place=True, hydration MUST keep the live in-memory ctx.

    manager.track_position(db_snapshot)

    # The same live object is still tracked (object identity preserved).
    refreshed = manager._find_position_by_id("pos-hydrate-1")  # type: ignore[attr-defined]
    assert refreshed is live

    # In-memory invariants preserved:
    assert live.current_quantity == 0.5
    assert live.tp_levels[0].status == "triggered"
    assert 0 in live.dispatched_sl_levels
    assert live.sl_exchange_order_id == "sl-new-after-tp1"

    # DB-shaped config was merged in:
    assert live.tp_levels[0].trigger_price == 101_500.0


async def test_hydration_replace_in_place_overrides_live_ctx() -> None:
    manager = _make_manager()
    live = _build_ctx(current_quantity=1.0, tp1_status="open", tp1_trigger=101_000.0)
    manager.track_position(live)

    fresh = _build_ctx(current_quantity=2.0, tp1_status="cancelled", tp1_trigger=200_000.0)
    manager.track_position(fresh, replace_in_place=True)

    refreshed = manager._find_position_by_id("pos-hydrate-1")  # type: ignore[attr-defined]
    assert refreshed is fresh
    assert fresh.current_quantity == 2.0
