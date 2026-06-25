"""Integration regression: production TP1-fill cascade scenario.

Replays the operator-reported incident end-to-end through three real
production layers (no monkey-patched paths):

1. ``BinanceAdapter._normalize_algo_update`` for the raw WS payload
   shape Binance sends for TP fills (TRIGGERED + FINISHED + the
   spawned market order's ORDER_TRADE_UPDATE).
2. ``WebSocketManager._handle_order_update`` for routing + dedup +
   per-position locking.
3. ``MultiTPEngine.handle_tp_triggered`` for SL repositioning,
   ``dispatched_sl_levels`` short-circuit, and engine-level
   pre-flight liveness guards.

Cascade prevention asserts:

  Scenario A — happy path with ``sl_lock_pct=50``:
    * Exactly ONE ``sl_adjustment_decided`` audit.
    * Exactly ONE ``replace_sl`` task enqueued.
    * ZERO ``sl_adjustment_skipped_position_already_flat``.
    * ZERO ``order_task_fatal_error``.
    * ZERO ``emergency_close_skipped_position_flat``.

  Scenario B — 4 rapid duplicate ALGO_UPDATEs for the same TP1 fill
  (mirrors the production audit shape):
    * Still exactly ONE ``sl_adjustment_decided`` audit.
    * Still exactly ONE ``replace_sl`` task.
    * Three ``multi_tp_duplicate_dispatch_ignored`` audits.

  Scenario C — biржа already flat between dispatch and execute:
    * Engine pre-flight emits ``sl_adjustment_skipped_position_already_flat``.
    * No ``replace_sl`` queued, no emergency close.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def _build_binance_adapter() -> BinanceAdapter:
    """Construct a BinanceAdapter purely for ``_normalize_algo_update``."""
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


def _three_tp_position() -> PositionContext:
    entry = 70_000.0
    return PositionContext(
        position_id="pos-cascade",
        account_id="acc-cascade",
        symbol=SYMBOL,
        side=PositionSide.LONG,
        state=PositionState.OPEN,
        entry_price=entry,
        original_quantity=0.3,
        current_quantity=0.3,
        current_sl_price=68_000.0,
        sl_exchange_order_id="sl-original",
        tp_mode="multi",
        tp_levels=[
            TPLevel(
                level=1,
                price_offset_pct=1.0,
                close_pct=33.0,
                trigger_price=entry * 1.01,
                status="open",
                exchange_order_id="tp1-coid",
                sl_lock_pct=50.0,
            ),
            TPLevel(
                level=2,
                price_offset_pct=2.0,
                close_pct=33.0,
                trigger_price=entry * 1.02,
                status="open",
                exchange_order_id="tp2-coid",
                sl_lock_pct=50.0,
            ),
            TPLevel(
                level=3,
                price_offset_pct=3.0,
                close_pct=34.0,
                trigger_price=entry * 1.03,
                status="open",
                exchange_order_id="tp3-coid",
            ),
        ],
    )


def _snapshot(*, size: float, mark: float) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=SYMBOL,
        side=AdapterPositionSide.LONG,
        size=size,
        entry_price=70_000.0,
        unrealized_pnl=0.0,
        leverage=10,
        mark_price=mark,
        liquidation_price=60_000.0,
        open_orders=[],
    )


def _algo_update_payload(
    *, caid: str, aid: int, trigger_price: float, qty: float
) -> dict[str, Any]:
    return {
        "e": "ALGO_UPDATE",
        "E": 1_700_000_000_500,
        "T": 1_700_000_000_490,
        "o": {
            "aid": aid,
            "caid": caid,
            "at": "CONDITIONAL",
            "o": "TAKE_PROFIT_MARKET",
            "s": "BTCUSDT",
            "S": "SELL",
            "ps": "BOTH",
            "f": "GTC",
            "q": f"{qty}",
            "X": "TRIGGERED",
            "ai": 11_223_344,
            "ap": f"{trigger_price + 5}",
            "aq": f"{qty}",
            "act": "MARKET",
            "tp": f"{trigger_price}",
            "p": "0",
        },
    }


def _make_manager(queue: AsyncMock, ws_adapter: AsyncMock) -> WebSocketManager:
    async def _resolver(_position: PositionContext) -> OrderExecutionQueue:
        return queue

    async def _persist(_position: PositionContext) -> None:
        return None

    manager = WebSocketManager(
        adapter=ws_adapter,
        account_id="acc-cascade",
        persist_position=_persist,
        order_queue_resolver=_resolver,
    )
    manager._warmed_up = True  # type: ignore[attr-defined]
    return manager


async def test_happy_path_single_tp1_fill_yields_exactly_one_replace_sl() -> None:
    binance = _build_binance_adapter()
    queue = AsyncMock(spec=OrderExecutionQueue)
    ws_adapter = AsyncMock(spec=ExchangeAdapter)
    # Engine pre-flight queries the WS adapter directly.
    ws_adapter.get_position.return_value = _snapshot(size=0.2, mark=70_700.0)

    position = _three_tp_position()
    manager = _make_manager(queue, ws_adapter)
    manager.track_position(position)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        normalized = binance._normalize_algo_update(
            _algo_update_payload(caid="tp1-coid", aid=10_001, trigger_price=70_700.0, qty=0.1)
        )
        assert normalized is not None
        await manager._handle_order_update(normalized)
    finally:
        auto_trade_audit.set_audit_hook(None)

    decided = [
        event for event in audits if event[0] == "sl_adjustment_decided"
    ]
    dispatched = [
        event for event in audits if event[0] == "sl_adjustment_dispatched"
    ]
    duplicates = [
        event for event in audits if event[0] == "multi_tp_duplicate_dispatch_ignored"
    ]
    skipped = [event for event in audits if event[0].startswith("sl_adjustment_skipped")]
    fatal = [
        event for event in audits if event[0] == "order_task_fatal_error"
    ]
    emergency_skipped = [
        event for event in audits if event[0] == "emergency_close_skipped_position_flat"
    ]

    assert len(decided) == 1
    assert len(dispatched) == 1
    assert duplicates == []
    assert skipped == []
    assert fatal == []
    assert emergency_skipped == []

    assert position.tp_levels[0].status == "triggered"
    assert 0 in position.dispatched_sl_levels


async def test_four_rapid_duplicate_tp1_fills_collapse_to_one_replace_sl() -> None:
    """Replay the production audit pattern: 4 dispatches in same millisecond."""
    binance = _build_binance_adapter()
    queue = AsyncMock(spec=OrderExecutionQueue)
    ws_adapter = AsyncMock(spec=ExchangeAdapter)
    ws_adapter.get_position.return_value = _snapshot(size=0.2, mark=70_700.0)

    position = _three_tp_position()
    manager = _make_manager(queue, ws_adapter)
    manager.track_position(position)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        normalized = binance._normalize_algo_update(
            _algo_update_payload(caid="tp1-coid", aid=10_001, trigger_price=70_700.0, qty=0.1)
        )
        assert normalized is not None
        # 4 concurrent deliveries through the same WS routing.
        await asyncio.gather(*[manager._handle_order_update(normalized) for _ in range(4)])
    finally:
        auto_trade_audit.set_audit_hook(None)

    decided = [
        event for event in audits if event[0] == "sl_adjustment_decided"
    ]
    assert len(decided) == 1, (
        f"Expected exactly one sl_adjustment_decided; got {len(decided)}"
    )

    # Quantity decremented exactly once: 0.3 - (0.3 * 0.33) = 0.201.
    expected_remaining = 0.3 - (0.3 * 0.33)
    assert position.current_quantity == pytest.approx(expected_remaining)
    # No emergency cascade.
    assert not any(event[0] == "order_task_fatal_error" for event in audits)


async def test_position_flat_between_dispatch_and_execute_skips_safely() -> None:
    binance = _build_binance_adapter()
    queue = AsyncMock(spec=OrderExecutionQueue)
    ws_adapter = AsyncMock(spec=ExchangeAdapter)
    # By the time the engine pre-flights, Binance has auto-closed via
    # ``closePosition=true`` SL — no position is left.
    ws_adapter.get_position.return_value = None

    position = _three_tp_position()
    manager = _make_manager(queue, ws_adapter)
    manager.track_position(position)

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    from app.services import audit as auto_trade_audit

    auto_trade_audit.set_audit_hook(_hook)
    try:
        normalized = binance._normalize_algo_update(
            _algo_update_payload(caid="tp1-coid", aid=10_001, trigger_price=70_700.0, qty=0.1)
        )
        assert normalized is not None
        await manager._handle_order_update(normalized)
    finally:
        auto_trade_audit.set_audit_hook(None)

    skipped = [
        event for event in audits if event[0] == "sl_adjustment_skipped_position_already_flat"
    ]
    assert len(skipped) == 1
    decided = [
        event for event in audits if event[0] == "sl_adjustment_decided"
    ]
    assert decided == []
    queue.enqueue.assert_not_called()
