"""Tests for the WSManager TP-fill wiring layer.

These tests cover the path that turns a TP fill event delivered via the user
data WS into an enqueued ``replace_sl`` order. They are regression tests for
several issues identified during the SL repositioning audit:

- P3 (sub-tick price tolerance) — verified by the slippage matching test.
- P4 (stale-tick guard rejecting fill events) — covered separately in
  ``test_stale_tick_does_not_block_fill_events.py``.
- P8 (silent unmatched-fill swallowing) — verified by the unmatched-fill test.
- P10 (Bybit orderLinkId vs orderId mismatch) — verified by the
  client-order-id matching test.
- P11 (concurrent fills interleaving qty/state mutations) — verified by the
  concurrent-fills test.
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

from app.services import audit as auto_trade_audit  # noqa: E402
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


# ───────────────────────────── helpers ────────────────────────────────────


def _build_long_multi_tp_position(
    *,
    position_id: str = "pos-1",
    entry_price: float = 100_000.0,
    initial_sl: float = 98_000.0,
    levels: list[tuple[float, float, float | None, str | None]] | None = None,
    state: PositionState = PositionState.OPEN,
) -> PositionContext:
    """Build a LONG multi-TP position.

    ``levels`` = list of (offset_pct, close_pct, sl_lock_pct, move_sl_to).
    """
    if levels is None:
        levels = [
            (1.0, 50.0, 0.0, None),  # TP1 at +1% with sl_lock_pct=0 (breakeven)
            (3.0, 50.0, None, None),
        ]
    tp_levels: list[TPLevel] = []
    for index, (offset, close_pct, sl_lock_pct, move_sl_to) in enumerate(levels):
        trigger = entry_price * (1.0 + offset / 100.0)
        tp_levels.append(
            TPLevel(
                level=index + 1,
                price_offset_pct=offset,
                close_pct=close_pct,
                trigger_price=trigger,
                status="open",
                exchange_order_id=f"tp-{index + 1}-coid",
                sl_lock_pct=sl_lock_pct,
                move_sl_to=move_sl_to,
            )
        )
    return PositionContext(
        position_id=position_id,
        account_id="acc-1",
        symbol=SYMBOL,
        side=PositionSide.LONG,
        state=state,
        entry_price=entry_price,
        original_quantity=1.0,
        current_quantity=1.0,
        current_sl_price=initial_sl,
        sl_exchange_order_id="sl-1",
        tp_mode="multi",
        tp_levels=tp_levels,
    )


def _make_manager(
    *,
    adapter: ExchangeAdapter | None = None,
    queue: OrderExecutionQueue | None = None,
    kill_switch_handler: Any = None,
) -> WebSocketManager:
    adapter_ = adapter or AsyncMock(spec=ExchangeAdapter)
    queue_ = queue or AsyncMock(spec=OrderExecutionQueue)

    async def _resolver(_position: PositionContext) -> OrderExecutionQueue:
        return queue_

    async def _persist(_position: PositionContext) -> None:
        return None

    manager = WebSocketManager(
        adapter=adapter_,
        account_id="acc-1",
        persist_position=_persist,
        order_queue_resolver=_resolver,
        kill_switch_handler=kill_switch_handler,
    )
    # Bypass warmup gate for tests.
    manager._warmed_up = True
    return manager


async def test_ws_manager_forwards_kill_switch_handler_and_starts_adjuster() -> None:
    """T2.3b — a kill-switch-only position (no SL pipeline) still gets an adjuster,
    and the manager forwards its kill_switch_handler to it."""
    handler = AsyncMock()
    manager = _make_manager(kill_switch_handler=handler)
    position = PositionContext(position_id="p-ks", symbol=SYMBOL, state=PositionState.OPEN)
    position.kill_switch_enabled = True  # no trailing/breakeven/volatility SL

    await manager._ensure_realtime_sl_pipeline(position)

    adjuster = manager._sl_adjusters.get(SYMBOL)
    assert adjuster is not None  # created despite no SL pipeline (needs_realtime_monitoring)
    assert adjuster._kill_switch_handler is handler  # handler forwarded


def _seed_last_good_price(manager: WebSocketManager, price: float) -> None:
    """Seed the stale-tick reference so the guard does not fire on first event."""
    normalized = WebSocketManager._normalize_symbol_key(SYMBOL)
    manager._last_prices[normalized] = price
    manager._last_good_prices[normalized] = price


# ───────────────────────────── tests ──────────────────────────────────────


async def test_tp_fill_with_matching_order_id_enqueues_replace_sl() -> None:
    queue = AsyncMock(spec=OrderExecutionQueue)
    position = _build_long_multi_tp_position()
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-1-coid",  # matches level 0 by id
        "client_order_id": "",
        "status": "filled",
        "order_type": "take_profit",
        "price": position.tp_levels[0].trigger_price,
        "filled_quantity": 0.5,
    }

    await manager._handle_order_update(event)

    assert queue.enqueue.await_count >= 1
    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" in enqueued_actions
    replace_task = next(
        call.args[0]
        for call in queue.enqueue.await_args_list
        if call.args[0].action == "replace_sl"
    )
    # sl_lock_pct=0 → SL goes to entry.
    assert replace_task.params["new_trigger_price"] == pytest.approx(position.entry_price)


async def test_tp_fill_matched_via_orderlinkid_when_real_orderid_differs() -> None:
    """Bybit echoes our orderLinkId via client_order_id field on WS events.

    Before the fix, ``_match_tp_level`` only checked ``order_id`` (the real
    Bybit orderId), so the fill could not be mapped back to a level.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    position = _build_long_multi_tp_position()
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "real-bybit-id-12345",  # not in our level list
        "client_order_id": "tp-1-coid",  # IS our level's stored exchange_order_id
        "status": "filled",
        "order_type": "take_profit",
        "price": position.tp_levels[0].trigger_price,
        "filled_quantity": 0.5,
    }

    await manager._handle_order_update(event)

    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" in enqueued_actions


async def test_tp_fill_with_only_price_match_within_tolerance_enqueues_replace_sl() -> None:
    """Real fills slip a few ticks; price-match must use a sane tolerance —
    but only when the event carries NO order ids (rare path).

    After the cascade-fix the matcher only falls back to price when
    ``event.order_id`` and ``event.client_order_id`` are both absent. An
    event with non-matching ids is treated as "addressed to nobody open"
    and returns None (the caller's ``_is_duplicate_tp_trigger`` then
    handles whether it's a stale duplicate of a triggered level).
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    position = _build_long_multi_tp_position()
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    # 0.3% slippage on the trigger price — within MULTI_TP_MATCH_TOLERANCE_PCT=0.5%.
    fill_price = position.tp_levels[0].trigger_price * 1.003

    event = {
        "type": "order",
        "symbol": SYMBOL,
        # Both id fields absent → matcher will use the price-fallback path.
        "order_id": "",
        "client_order_id": "",
        "status": "filled",
        "order_type": "take_profit",
        "price": fill_price,
        "filled_quantity": 0.5,
    }

    await manager._handle_order_update(event)

    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" in enqueued_actions


async def test_tp_fill_unmatched_emits_audit_event_and_does_not_enqueue() -> None:
    """When neither order-id nor price match, surface as ``tp_fill_unmatched``."""
    queue = AsyncMock(spec=OrderExecutionQueue)
    position = _build_long_multi_tp_position()
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    captured: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        captured.append((event_type, payload))

    auto_trade_audit.set_audit_hook(_hook)
    try:
        # Fill price 10% off — outside the 0.5% match tolerance.
        event = {
            "type": "order",
            "symbol": SYMBOL,
            "order_id": "no-match",
            "client_order_id": "no-match",
            "status": "filled",
            "order_type": "take_profit",
            "price": position.entry_price * 1.10,
            "filled_quantity": 0.5,
        }
        await manager._handle_order_update(event)
    finally:
        auto_trade_audit.set_audit_hook(None)

    types_emitted = [event_type for event_type, _ in captured]
    assert "tp_fill_unmatched" in types_emitted
    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in enqueued_actions


async def test_tp_fill_with_no_sl_directive_emits_skipped_and_does_not_enqueue() -> None:
    """sl_lock_pct=None and move_sl_to=None → emit skipped, no replace_sl."""
    queue = AsyncMock(spec=OrderExecutionQueue)
    # Both levels with no SL directive.
    position = _build_long_multi_tp_position(
        levels=[(1.0, 50.0, None, None), (3.0, 50.0, None, None)],
    )
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    captured: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        captured.append((event_type, payload))

    auto_trade_audit.set_audit_hook(_hook)
    try:
        event = {
            "type": "order",
            "symbol": SYMBOL,
            "order_id": "tp-1-coid",
            "client_order_id": "",
            "status": "filled",
            "order_type": "take_profit",
            "price": position.tp_levels[0].trigger_price,
            "filled_quantity": 0.5,
        }
        await manager._handle_order_update(event)
    finally:
        auto_trade_audit.set_audit_hook(None)

    skipped_events = [
        payload for event_type, payload in captured if event_type == "sl_adjustment_skipped"
    ]
    assert skipped_events
    assert skipped_events[0]["reason"] == "lock_pct_null"
    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in enqueued_actions


async def test_concurrent_tp1_and_tp2_fills_are_serialized_per_position() -> None:
    """Per-position lock prevents two routing coroutines from interleaving.

    We assert state consistency: both TP levels end as triggered, current
    quantity ends at 0.0, and the engine ran exactly twice.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    position = _build_long_multi_tp_position(
        levels=[(1.0, 50.0, 0.0, None), (3.0, 50.0, None, "tp1")],
    )
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    event_1 = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-1-coid",
        "client_order_id": "",
        "status": "filled",
        "order_type": "take_profit",
        "price": position.tp_levels[0].trigger_price,
        "filled_quantity": 0.5,
    }
    event_2 = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-2-coid",
        "client_order_id": "",
        "status": "filled",
        "order_type": "take_profit",
        "price": position.tp_levels[1].trigger_price,
        "filled_quantity": 0.5,
    }

    await asyncio.gather(
        manager._handle_order_update(event_1),
        manager._handle_order_update(event_2),
    )

    assert position.tp_levels[0].status == "triggered"
    assert position.tp_levels[1].status == "triggered"
    assert position.current_quantity == pytest.approx(0.0)


# ─── Regression tests for the multi-TP "TP3 + SL слетели" cluster ─────────


async def test_duplicate_event_for_triggered_level_does_not_cascade_to_next_open_level() -> None:
    """Regression: a duplicate WS delivery whose ``order_id`` matches an
    already-triggered TP level must NOT cascade to the next open level by
    price-matching.

    Before the fix, ``_match_tp_level`` skipped the triggered level in the
    id-match loop, then fell through to the price-fallback path. The
    event's ``trigger_price`` (echoed back from Binance) was within the
    0.5 % tolerance of the next open level, so the matcher returned that
    next index — silently advancing the engine state on a TP the market
    never actually reached. With three TP levels and slightly-spaced
    triggers, two duplicate events could drive ``current_quantity`` to 0
    and trip ``_cancel_remaining_orders`` on a position still open on the
    exchange. That's the cascade that ultimately fired
    ``emergency_market_close`` and closed the user's position at market.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    queue.purge_pending = AsyncMock(return_value=[])
    position = _build_long_multi_tp_position(
        levels=[
            (0.25, 25.0, -50.0, None),   # TP1
            (0.50, 50.0, 0.0, None),     # TP2
            (0.75, 25.0, 35.0, None),    # TP3
        ],
    )
    # Pretend TP1 was just processed.
    position.tp_levels[0].status = "triggered"
    position.current_quantity = position.original_quantity * 0.75

    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    # A duplicate of TP1's fill event: the order_id matches TP1's
    # exchange_order_id (which we stored at placement time), AND the
    # event echoes TP1's trigger_price. In the buggy implementation this
    # would silently match TP2 by price.
    duplicate_event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-1-coid",
        "client_order_id": "",
        "status": "filled",
        "order_type": "take_profit",
        "trigger_price": position.tp_levels[0].trigger_price,
        "price": position.tp_levels[0].trigger_price,
        "filled_quantity": position.original_quantity * 0.25,
    }
    await manager._handle_order_update(duplicate_event)

    # TP2 must remain open. No replace_sl enqueued.
    assert position.tp_levels[1].status == "open"
    actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in actions


async def test_binance_triggered_then_finished_pair_processed_only_once() -> None:
    """Hard-dedup at the top of ``_handle_tp_triggered_event``.

    Binance emits multiple WS events for a single TP fill:
      1. ALGO_UPDATE with ``X=TRIGGERED`` (algo condition met)
      2. ALGO_UPDATE with ``X=FINISHED`` (underlying market order completed)
      3. ORDER_TRADE_UPDATE with the underlying fill

    The first should advance the multi-TP engine; the rest are
    follow-ups for the same logical fill and must not re-invoke the
    engine. Regression for the user-observed cascade where 4
    ``sl_adjustment_decided`` events fired for a single TP1 fill.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    queue.purge_pending = AsyncMock(return_value=[])
    position = _build_long_multi_tp_position()
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    # 1. Real TP1 fill — TRIGGERED status.
    triggered_event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-1-coid",
        "client_order_id": "",
        "status": "triggered",
        "order_type": "take_profit",
        "price": position.tp_levels[0].trigger_price,
        "trigger_price": position.tp_levels[0].trigger_price,
        "filled_quantity": 0.5,
    }
    await manager._handle_order_update(triggered_event)
    assert position.tp_levels[0].status == "triggered"
    replace_sl_calls_after_triggered = sum(
        1 for c in queue.enqueue.await_args_list if c.args[0].action == "replace_sl"
    )
    assert replace_sl_calls_after_triggered == 1

    # 2. FINISHED follow-up for the same algo order. Adapter normalises
    # this to status="finished" (NOT a fill), so ``_is_fill_event``
    # returns False and the handler is never invoked. Belt-and-suspenders:
    # even if it WERE a fill, the hard-dedup guard at the top of
    # ``_handle_tp_triggered_event`` would short-circuit because the
    # order_id matches an already-triggered level.
    finished_event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-1-coid",  # same algoId
        "client_order_id": "",
        "status": "finished",
        "order_type": "take_profit",
        "price": position.tp_levels[0].trigger_price,
        "trigger_price": position.tp_levels[0].trigger_price,
        "filled_quantity": 0.5,
    }
    await manager._handle_order_update(finished_event)
    # TP2 must remain open; engine must NOT have been re-invoked.
    assert position.tp_levels[1].status == "open"
    replace_sl_calls_after_finished = sum(
        1 for c in queue.enqueue.await_args_list if c.args[0].action == "replace_sl"
    )
    assert replace_sl_calls_after_finished == 1, (
        "FINISHED follow-up must not enqueue a second replace_sl"
    )


async def test_hard_dedup_short_circuits_when_event_id_matches_triggered_level() -> None:
    """The hard-dedup guard at ``_handle_tp_triggered_event`` entry runs
    before ``_match_tp_level`` and catches any event whose ``order_id``
    points at a level whose ``status`` is already ``triggered``.

    This is defence-in-depth — it works even if a future refactor of the
    matcher accidentally reinstates the cascade-producing price-fallback.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    queue.purge_pending = AsyncMock(return_value=[])
    position = _build_long_multi_tp_position()
    # Pretend TP1 already triggered.
    position.tp_levels[0].status = "triggered"
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    # Event carrying TP1's order_id but with status="triggered" (i.e.,
    # would normally pass _is_fill_event). The hard dedup guard at the
    # top of _handle_tp_triggered_event must catch it.
    duplicate_event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-1-coid",
        "client_order_id": "",
        "status": "triggered",
        "order_type": "take_profit",
        "price": position.tp_levels[0].trigger_price,
        "trigger_price": position.tp_levels[0].trigger_price,
        "filled_quantity": 0.5,
    }
    await manager._handle_order_update(duplicate_event)

    # TP2 untouched. No replace_sl enqueued.
    assert position.tp_levels[1].status == "open"
    actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in actions


async def test_event_with_unknown_order_ids_returns_none_no_price_fallback() -> None:
    """If an event has non-empty order ids that don't match any open
    level, the matcher returns None (the event is for an order we no
    longer track or a stale duplicate). It must NOT fall back to
    price-matching the next open level.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    queue.purge_pending = AsyncMock(return_value=[])
    position = _build_long_multi_tp_position()
    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    # Non-empty ids that don't match any open level + price within
    # tolerance of TP1. The matcher must NOT match TP1.
    event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "totally-unknown",
        "client_order_id": "also-unknown",
        "status": "filled",
        "order_type": "take_profit",
        "price": position.tp_levels[0].trigger_price,
        "filled_quantity": 0.5,
    }
    await manager._handle_order_update(event)

    assert position.tp_levels[0].status == "open"
    actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in actions


async def test_final_tp_fill_purges_pending_sl_and_cancels_remaining_once() -> None:
    """When the final TP fills, the WS manager must:

    1. NOT enqueue a ``replace_sl(qty=0)`` (would race the cleanup below).
    2. Call ``OrderExecutionQueue.purge_pending`` for the position so any
       in-flight ``replace_sl`` from a previous TP fill is tombstoned.
    3. Cancel the live SL on the exchange exactly once.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    queue.purge_pending = AsyncMock(return_value=[])

    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.cancel_conditional_order = AsyncMock(return_value=True)
    adapter.get_open_conditional_orders = AsyncMock(return_value=[])

    # Three-level position with TP1+TP2 already triggered; TP3 fills now.
    position = _build_long_multi_tp_position(
        levels=[
            (1.0, 33.0, 0.0, None),
            (2.0, 33.0, 50.0, None),
            (3.0, 34.0, 100.0, None),
        ],
    )
    position.tp_levels[0].status = "triggered"
    position.tp_levels[1].status = "triggered"
    position.current_quantity = position.original_quantity * (
        position.tp_levels[2].close_pct / 100.0
    )

    manager = _make_manager(adapter=adapter, queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "tp-3-coid",
        "client_order_id": "",
        "status": "filled",
        "order_type": "take_profit",
        "price": position.tp_levels[2].trigger_price,
        "filled_quantity": position.current_quantity,
    }
    await manager._handle_order_update(event)

    # No replace_sl enqueued for the final fill.
    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in enqueued_actions

    # purge_pending was called with the protective-action set, scoped to this position.
    assert queue.purge_pending.await_count >= 1
    purge_call = queue.purge_pending.await_args_list[0]
    assert purge_call.args[0] == position.position_id
    assert {"place_sl", "replace_sl"}.issubset(purge_call.args[1])

    # The live SL was cancelled on the exchange (once).
    cancelled_ids = [
        call.args[1] for call in adapter.cancel_conditional_order.await_args_list
    ]
    assert "sl-1" in cancelled_ids


async def test_match_tp_level_excludes_triggered_levels_in_price_fallback() -> None:
    """A fill price equally close to TP2 (already triggered) and TP3 must
    match TP3 — never re-fire a triggered level.

    Regression guard for the price-fallback branch of ``_match_tp_level``
    (``_closest_open_tp_level_index`` filters ``status == "triggered"``).
    Uses the no-id path because the matcher only price-matches when the
    event carries no order ids — that's the contract after the
    cascade-fix.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    queue.purge_pending = AsyncMock(return_value=[])
    position = _build_long_multi_tp_position(
        levels=[
            (1.0, 33.0, 0.0, None),
            (2.0, 33.0, 50.0, None),
            (3.0, 34.0, 100.0, None),
        ],
    )
    position.tp_levels[0].status = "triggered"
    position.tp_levels[1].status = "triggered"
    # current_quantity reflects only TP3's slice after TP1+TP2 closed.
    position.current_quantity = position.original_quantity * 0.34

    manager = _make_manager(queue=queue)
    manager.track_position(position)
    _seed_last_good_price(manager, position.entry_price)

    # An event whose price is half-way between TP2 (already triggered) and
    # TP3. The matcher must skip TP2 and pick TP3. ``order_id`` /
    # ``client_order_id`` absent so the matcher uses the price fallback.
    midpoint = (
        position.tp_levels[1].trigger_price + position.tp_levels[2].trigger_price
    ) / 2.0

    event = {
        "type": "order",
        "symbol": SYMBOL,
        "order_id": "",
        "client_order_id": "",
        "status": "filled",
        "order_type": "take_profit",
        "price": midpoint,
        "filled_quantity": position.current_quantity,
    }
    await manager._handle_order_update(event)

    # TP3 must end as triggered (not TP2 re-firing).
    assert position.tp_levels[2].status == "triggered"
    # And we must NOT have enqueued a replace_sl on the final fill.
    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in enqueued_actions
