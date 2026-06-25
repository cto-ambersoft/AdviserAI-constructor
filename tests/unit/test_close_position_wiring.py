"""End-to-end wiring of ``close_position=True`` from queue → adapter → WS.

Three orthogonal wiring contracts must hold:

1. ``OrderExecutionQueue`` routes the ``close_position`` param from the
   ``place_sl`` task into ``adapter.place_stop_loss(...)``.
2. ``WebSocketManager._enqueue_initial_protection_orders`` enqueues the
   initial SL with ``close_position=True``.
3. Both adapters accept the flag (Bybit treats it as a no-op since
   ``tpslMode: "Full"`` is already equivalent).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import ExchangeAdapter, OrderSide  # noqa: E402
from app.services.position.context import (  # noqa: E402
    PositionContext,
    PositionSide,
)
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
)
from app.services.position.state_machine import PositionState  # noqa: E402
from app.services.ws.manager import WebSocketManager  # noqa: E402

SYMBOL = "BTC/USDT:USDT"


# ───────────────────────── 1. queue routing ───────────────────────────────


async def test_order_queue_passes_close_position_true_to_adapter_place_stop_loss() -> None:
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return AsyncMock()

    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.place_stop_loss = _capture  # type: ignore[method-assign]

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")

    import asyncio

    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.NEW_CONDITIONAL,
            created_at=1.0,
            position_id="pos-1",
            action="place_sl",
            params={
                "symbol": SYMBOL,
                "side": OrderSide.SELL,
                "quantity": 0.1,
                "trigger_price": 68_000.0,
                "client_order_id": "cid-cp-1",
                "reduce_only": True,
                "close_position": True,
            },
        )
    )

    processor = asyncio.create_task(queue.start_processing())
    await asyncio.wait_for(queue._queue.join(), timeout=2.0)
    await queue.stop()
    await asyncio.wait_for(processor, timeout=1.0)

    assert captured.get("close_position") is True


async def test_order_queue_defaults_close_position_to_false_when_not_provided() -> None:
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return AsyncMock()

    adapter = AsyncMock(spec=ExchangeAdapter)
    adapter.can_place_order.return_value = True
    adapter.place_stop_loss = _capture  # type: ignore[method-assign]

    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-2")

    import asyncio

    await queue.enqueue(
        OrderTask(
            priority=OrderPriority.NEW_CONDITIONAL,
            created_at=1.0,
            position_id="pos-2",
            action="place_sl",
            params={
                "symbol": SYMBOL,
                "side": OrderSide.SELL,
                "quantity": 0.1,
                "trigger_price": 68_000.0,
                "client_order_id": "cid-q-1",
                "reduce_only": True,
                # close_position omitted
            },
        )
    )

    processor = asyncio.create_task(queue.start_processing())
    await asyncio.wait_for(queue._queue.join(), timeout=2.0)
    await queue.stop()
    await asyncio.wait_for(processor, timeout=1.0)

    assert captured.get("close_position") is False


# ───────────── 2. WSManager initial SL placement ─────────────────────────


async def test_ws_manager_initial_sl_uses_close_position_true() -> None:
    """When entry fills, the initial protective SL goes in with closePosition=true."""
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

    position = PositionContext(
        position_id="pos-init-sl",
        account_id="acc-1",
        symbol=SYMBOL,
        side=PositionSide.LONG,
        state=PositionState.OPEN,
        entry_price=70_000.0,
        original_quantity=0.1,
        current_quantity=0.1,
        current_sl_price=68_000.0,
        tp_mode="single",
        current_tp_price=72_000.0,
    )

    await manager._enqueue_initial_protection_orders(position)

    sl_calls = [
        call.args[0]
        for call in queue.enqueue.await_args_list
        if call.args[0].action == "place_sl"
    ]
    assert sl_calls, "initial place_sl task was not enqueued"
    sl_task = sl_calls[0]
    assert sl_task.params["close_position"] is True


async def test_ws_manager_emergency_sl_after_disconnect_uses_close_position_true() -> None:
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

    position = PositionContext(
        position_id="pos-emergency",
        account_id="acc-1",
        symbol=SYMBOL,
        side=PositionSide.LONG,
        state=PositionState.RECONNECTING,
        entry_price=70_000.0,
        original_quantity=0.1,
        current_quantity=0.1,
        current_sl_price=68_000.0,
    )

    await manager._enqueue_emergency_sl(position)

    assert queue.enqueue.await_count == 1
    task = queue.enqueue.await_args_list[0].args[0]
    assert task.action == "place_sl"
    assert task.params["close_position"] is True
    assert task.priority == OrderPriority.EMERGENCY_SL


# ─────────────── 3. Bybit accepts the flag without breaking ──────────────


async def test_bybit_place_stop_loss_accepts_close_position_flag() -> None:
    """Bybit's tpslMode='Full' is already close-position-equivalent.

    The adapter must accept the parameter for interface parity but it's a
    no-op in the request body — the existing trading-stop call already
    closes the entire position at trigger.
    """
    import re
    from unittest.mock import MagicMock

    from aioresponses import aioresponses

    from app.services.exchange.bybit_adapter import BybitAdapter

    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(return_value=[])
    exchange.options = {"enableDemoTrading": False}
    exchange.isSandboxModeEnabled = False
    exchange.urls = {
        "api": {"private": "https://api.bybit.com", "public": "https://api.bybit.com"}
    }
    exchange.markets = {"BTC/USDT:USDT": {"precision": {"amount": 3, "price": 1}}}
    exchange.load_markets = AsyncMock(return_value=exchange.markets)
    exchange.amount_to_precision = lambda _s, q: format(
        float(int(float(q) * 1000)) / 1000.0, ".3f"
    )
    exchange.price_to_precision = lambda _s, p: format(round(float(p), 1), ".1f")

    adapter = BybitAdapter(
        ccxt_exchange=exchange,
        api_key="k",
        api_secret="s",
        rate_limiter=MagicMock(),
        mode="real",
    )

    trading_stop_url = re.compile(r"^https://api\.bybit\.com/v5/position/trading-stop$")
    with aioresponses() as mocked:
        mocked.post(
            trading_stop_url,
            status=200,
            payload={"retCode": 0, "result": {}},
        )

        result = await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.1,
            trigger_price=68_000.0,
            client_order_id="bybit-cp",
            close_position=True,  # accepted, no-op (tpslMode Full already)
        )

    assert result.order_type == "stop_loss"
    # Sanity: still made one trading-stop call.
    total = sum(
        1
        for (method, url) in mocked.requests.keys()
        if method == "POST" and "trading-stop" in str(url)
    )
    assert total == 1
