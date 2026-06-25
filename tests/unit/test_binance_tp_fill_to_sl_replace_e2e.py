"""End-to-end: a real Binance ``ALGO_UPDATE`` payload triggers SL repositioning.

Wires together three layers exactly as production does:

1. ``BinanceAdapter._normalize_algo_update`` — turns the raw WS payload into
   our internal event shape.
2. ``WebSocketManager._handle_order_update`` — runs the warmup/stale-tick
   guard and routes to ``_handle_tp_triggered_event``.
3. ``MultiTPEngine.handle_tp_triggered`` — computes the new SL price from
   ``sl_lock_pct`` and enqueues ``replace_sl``.

Before the Binance fix, step 1 left ``order_id`` empty and ``trigger_price``
zero — step 2 then could not match the level and silently returned True,
so step 3 never ran. This test fails with the old normalizer and passes
with the new one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import ExchangeAdapter  # noqa: E402
from app.services.exchange.binance_adapter import BinanceAdapter  # noqa: E402
from app.services.position.context import (  # noqa: E402
    PositionContext,
    PositionSide,
    TPLevel,
)
from app.services.position.order_queue import OrderExecutionQueue  # noqa: E402
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.ws.manager import WebSocketManager  # noqa: E402

SYMBOL = "BTC/USDT:USDT"


def _build_binance_adapter_for_normalization() -> BinanceAdapter:
    """Construct an adapter just to call _normalize_algo_update; no network."""
    exchange = MagicMock()
    exchange.options = {"enableDemoTrading": False}
    exchange.isSandboxModeEnabled = False
    exchange.urls = {
        "api": {"private": "https://fapi.binance.com", "public": "https://fapi.binance.com"}
    }
    exchange.markets = {"BTC/USDT:USDT": {"precision": {"amount": 3, "price": 1}}}
    return BinanceAdapter(
        ccxt_exchange=exchange,
        api_key="k",
        api_secret="s",
        rate_limiter=MagicMock(),
        mode="real",
    )


def _build_long_position(*, level_coid: str = "level-1-coid") -> PositionContext:
    entry = 70_000.0
    return PositionContext(
        position_id="pos-binance-1",
        account_id="acc-binance",
        symbol=SYMBOL,
        side=PositionSide.LONG,
        state=PositionState.OPEN,
        entry_price=entry,
        original_quantity=0.1,
        current_quantity=0.1,
        current_sl_price=68_000.0,
        sl_exchange_order_id="sl-algo-id-1",
        tp_mode="multi",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=50.0,
                trigger_price=entry * 1.01,  # 70700
                status="open",
                exchange_order_id=level_coid,
                sl_lock_pct=0.0,  # → SL to entry on TP1 fill
            ),
            TPLevel(
                level=2,
                price_offset_pct=3.0,
                close_pct=50.0,
                trigger_price=entry * 1.03,  # 72100
                status="open",
                exchange_order_id="level-2-coid",
            ),
        ],
    )


def _make_ws_manager(queue: AsyncMock) -> WebSocketManager:
    async def _resolver(_position: PositionContext) -> OrderExecutionQueue:
        return queue

    async def _persist(_position: PositionContext) -> None:
        return None

    manager = WebSocketManager(
        adapter=AsyncMock(spec=ExchangeAdapter),
        account_id="acc-binance",
        persist_position=_persist,
        order_queue_resolver=_resolver,
    )
    manager._warmed_up = True  # bypass 4s warmup window in tests
    return manager


async def test_binance_tp1_algo_update_repositions_sl_to_breakeven() -> None:
    binance = _build_binance_adapter_for_normalization()
    queue = AsyncMock(spec=OrderExecutionQueue)
    position = _build_long_position()
    manager = _make_ws_manager(queue)
    manager.track_position(position)

    # Seed last_good_prices so the stale-tick guard does not interfere
    # (independently — fill events bypass it now anyway).
    normalized_symbol = WebSocketManager._normalize_symbol_key(SYMBOL)
    manager._last_prices[normalized_symbol] = position.entry_price
    manager._last_good_prices[normalized_symbol] = position.entry_price

    # Realistic Binance USDS-M Futures ALGO_UPDATE for TP1 fill.
    raw_ws_payload = {
        "e": "ALGO_UPDATE",
        "E": 1700_000_000_500,
        "T": 1700_000_000_490,
        "o": {
            "aid": 87_654_321,
            "caid": "level-1-coid",  # matches our locally stored exchange_order_id
            "at": "CONDITIONAL",
            "o": "TAKE_PROFIT_MARKET",
            "s": "BTCUSDT",
            "S": "SELL",
            "ps": "BOTH",
            "f": "GTC",
            "q": "0.05",
            # The TRIGGERED event is the one that carries the actual TP fill
            # for the engine. Binance also emits FINISHED afterwards, but that
            # is intentionally normalised to ``"finished"`` (NOT a fill) so
            # the engine cannot be invoked twice for the same TP — see
            # ``test_algo_update_status_finished_normalizes_to_distinct_finished``.
            "X": "TRIGGERED",
            "ai": 11_223_344,
            "ap": "70720.5",  # average fill price (slight slippage)
            "aq": "0.05",
            "act": "MARKET",
            "tp": "70700.0",  # configured trigger price
            "p": "0",
        },
    }

    # 1. Adapter normalizes the raw WS payload into our internal event.
    normalized_event = binance._normalize_algo_update(raw_ws_payload)
    assert normalized_event is not None
    # Critical: order_id and trigger_price must be populated; this is the
    # exact regression that broke SL repositioning.
    assert normalized_event["order_id"] == "87654321"
    assert normalized_event["client_order_id"] == "level-1-coid"
    assert normalized_event["trigger_price"] == pytest.approx(70_700.0)
    assert normalized_event["filled_quantity"] == pytest.approx(0.05)
    assert normalized_event["order_type"] == "take_profit"
    assert normalized_event["status"] == "triggered"

    # 2. The normalized event flows through WSManager.
    await manager._handle_order_update(normalized_event)

    # 3. The multi-TP engine has run, advanced the level, and queued replace_sl.
    assert position.tp_levels[0].status == "triggered"
    assert position.current_quantity == pytest.approx(0.05)

    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" in enqueued_actions
    replace_sl_task = next(
        call.args[0]
        for call in queue.enqueue.await_args_list
        if call.args[0].action == "replace_sl"
    )
    # sl_lock_pct=0 on TP1 → new SL at entry price (breakeven).
    assert replace_sl_task.params["new_trigger_price"] == pytest.approx(position.entry_price)
    # New SL covers the remaining quantity (0.05 = 50% of 0.1 original).
    assert replace_sl_task.params["new_quantity"] == pytest.approx(0.05)


async def test_binance_tp_fill_matched_via_caid_when_aid_unknown() -> None:
    """If we only stored the client algo id (caid) locally, matching still works."""
    binance = _build_binance_adapter_for_normalization()
    queue = AsyncMock(spec=OrderExecutionQueue)

    # The position stores ONLY the caid as exchange_order_id (e.g. when REST
    # response did not echo aid back; rare but possible during early WS
    # races).
    position = _build_long_position(level_coid="caid-tp1")
    manager = _make_ws_manager(queue)
    manager.track_position(position)
    manager._last_good_prices[WebSocketManager._normalize_symbol_key(SYMBOL)] = position.entry_price

    raw_ws_payload = {
        "e": "ALGO_UPDATE",
        "o": {
            "aid": 99_999_999,  # different from what we stored locally
            "caid": "caid-tp1",  # MATCH on this path
            "o": "TAKE_PROFIT_MARKET",
            "s": "BTCUSDT",
            "S": "SELL",
            "X": "FINISHED",
            "tp": "70700",
            "aq": "0.05",
            "q": "0.05",
        },
    }

    normalized_event = binance._normalize_algo_update(raw_ws_payload)
    assert normalized_event is not None
    await manager._handle_order_update(normalized_event)

    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" in enqueued_actions


async def test_binance_old_buggy_normalizer_would_have_failed() -> None:
    """Document the regression: old field codes leave order_id empty.

    This test models what the old normalizer would produce (using the
    ORDER_TRADE_UPDATE field codes by mistake) and verifies that the
    WSManager match fails — confirming this was the failure mode.
    """
    queue = AsyncMock(spec=OrderExecutionQueue)
    position = _build_long_position()
    manager = _make_ws_manager(queue)
    manager.track_position(position)
    manager._last_good_prices[WebSocketManager._normalize_symbol_key(SYMBOL)] = position.entry_price

    # Simulate what the old normalizer produced from a real ALGO_UPDATE.
    old_buggy_event: dict[str, Any] = {
        "type": "ALGO_UPDATE",
        "event_type": "ALGO_UPDATE",
        "symbol": "BTCUSDT",
        "order_id": "",  # blank: old code looked at "i"/"algoId", not "aid"
        "client_order_id": "",  # blank: old code looked at "c"/"clientAlgoId"
        "status": "triggered",
        "raw_status": "FINISHED",
        "execution_type": "",
        "order_type": "take_profit",
        "raw_order_type": "TAKE_PROFIT_MARKET",
        "price": 0.0,
        "average_price": 0.0,
        "trigger_price": 0.0,  # zero: old code looked at "sp", not "tp"
        "quantity": 0.05,
        "filled_quantity": 0.0,  # zero: old code looked at "z", not "aq"
        "side": "sell",
        "is_algo": True,
        "raw": {},
    }

    await manager._handle_order_update(old_buggy_event)

    # No replace_sl was enqueued — this is the bug. The level didn't match
    # by id (empty) or price (zero), and there was no fallback that would
    # have moved the SL.
    enqueued_actions = [call.args[0].action for call in queue.enqueue.await_args_list]
    assert "replace_sl" not in enqueued_actions
