"""Manual smoke test for Binance Futures demo trading via the custom BinanceAdapter.

Required environment:
- BINANCE_DEMO_API_KEY
- BINANCE_DEMO_API_SECRET
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.services.exchange.adapter import (  # noqa: E402
    ConditionalOrderResult,
    OrderSide,
    PositionSide,
    PositionSnapshot,
)
from app.services.exchange.binance_adapter import BinanceAdapter  # noqa: E402
from app.services.exchange.rate_limiter import AdaptiveRateLimiter  # noqa: E402

pytestmark = pytest.mark.exchange_demo

SYMBOL = "BTC/USDT:USDT"
POSITION_TIMEOUT_SECONDS = 45.0
ORDER_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 1.0
MIN_NOTIONAL_BUFFER = Decimal("1.05")


def _client_order_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}"[:36]


def _close_side(position: PositionSnapshot) -> OrderSide:
    return OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY


def _sl_price(position: PositionSnapshot, multiplier: float) -> float:
    basis = position.entry_price or position.mark_price
    if basis <= 0:
        pytest.fail(f"Unable to derive stop-loss price from position snapshot: {position}")
    return round(basis * multiplier, 2)


def _filter_value(filters: list[dict[str, Any]], filter_type: str, field: str) -> Decimal | None:
    for item in filters:
        if item.get("filterType") == filter_type and item.get(field) is not None:
            return Decimal(str(item[field]))
    return None


async def _entry_quantity(exchange: Any) -> float:
    market = exchange.market(SYMBOL)
    ticker = await exchange.fetch_ticker(SYMBOL)
    filters = market.get("info", {}).get("filters", [])
    if not isinstance(filters, list):
        filters = []

    min_cost = Decimal(str(market.get("limits", {}).get("cost", {}).get("min") or "100"))
    min_amount = Decimal(str(market.get("limits", {}).get("amount", {}).get("min") or "0.0001"))
    step_size = _filter_value(filters, "LOT_SIZE", "stepSize") or min_amount
    last_price = Decimal(
        str(
            ticker.get("last") or ticker.get("close") or ticker.get("bid") or ticker.get("ask") or 0
        )
    )
    if last_price <= 0:
        pytest.fail(f"Unable to derive {SYMBOL} price for entry sizing: {ticker}")

    raw_quantity = (min_cost * MIN_NOTIONAL_BUFFER) / last_price
    stepped_quantity = ((raw_quantity / step_size).to_integral_value(rounding=ROUND_UP)) * step_size
    quantity = max(min_amount, stepped_quantity)
    return float(exchange.amount_to_precision(SYMBOL, float(quantity)))


async def _wait_for(
    description: str,
    predicate: Any,
    *,
    timeout: float,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    last_value: Any = None
    while True:
        last_value = await predicate()
        if last_value:
            return last_value
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"Timed out waiting for {description}. Last value: {last_value!r}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _wait_for_position(adapter: BinanceAdapter) -> PositionSnapshot:
    async def _fetch() -> PositionSnapshot | None:
        return await adapter.get_position(SYMBOL)

    result = await _wait_for(
        f"an open {SYMBOL} position",
        _fetch,
        timeout=POSITION_TIMEOUT_SECONDS,
    )
    assert isinstance(result, PositionSnapshot)
    return result


async def _wait_for_stop_order(
    adapter: BinanceAdapter,
    *,
    expected_order_id: str,
    absent_order_id: str | None = None,
) -> ConditionalOrderResult:
    async def _find() -> ConditionalOrderResult | None:
        orders = await adapter.get_open_conditional_orders(SYMBOL)
        order_ids = {order.exchange_order_id for order in orders}
        if absent_order_id and absent_order_id in order_ids:
            return None
        for order in orders:
            if order.exchange_order_id == expected_order_id:
                return order
        return None

    result = await _wait_for(
        f"stop-loss order {expected_order_id}",
        _find,
        timeout=ORDER_TIMEOUT_SECONDS,
    )
    assert isinstance(result, ConditionalOrderResult)
    return result


async def _cancel_all_conditional_orders(adapter: BinanceAdapter) -> None:
    for order in await adapter.get_open_conditional_orders(SYMBOL):
        await adapter.cancel_conditional_order(SYMBOL, order.exchange_order_id)


async def _wait_for_position_closed(adapter: BinanceAdapter) -> None:
    async def _is_closed() -> bool:
        return await adapter.get_position(SYMBOL) is None

    await _wait_for(
        f"{SYMBOL} position to close",
        _is_closed,
        timeout=POSITION_TIMEOUT_SECONDS,
    )


async def _normalize_symbol_state(adapter: BinanceAdapter) -> None:
    await _cancel_all_conditional_orders(adapter)
    position = await adapter.get_position(SYMBOL)
    if position is None:
        return
    await adapter.partial_close(
        symbol=SYMBOL,
        side=_close_side(position),
        quantity=position.size,
        client_order_id=_client_order_id("smoke-pre-clean-close"),
    )
    await _wait_for_position_closed(adapter)
    await _cancel_all_conditional_orders(adapter)


async def _cleanup_position(adapter: BinanceAdapter) -> None:
    position = await adapter.get_position(SYMBOL)
    if position is None:
        return
    await adapter.partial_close(
        symbol=SYMBOL,
        side=_close_side(position),
        quantity=position.size,
        client_order_id=_client_order_id("smoke-clean-close"),
    )


@pytest.mark.asyncio
async def test_binance_demo_smoke() -> None:
    if os.getenv("CI"):
        pytest.skip("Manual-only demo smoke test is skipped in CI.")

    api_key = os.getenv("BINANCE_DEMO_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_DEMO_API_SECRET", "").strip()
    if not api_key or not api_secret:
        pytest.skip("BINANCE_DEMO_API_KEY and BINANCE_DEMO_API_SECRET are required.")

    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
    )
    exchange.enable_demo_trading(True)

    adapter = BinanceAdapter(
        ccxt_exchange=exchange,
        api_key=api_key,
        api_secret=api_secret,
        rate_limiter=AdaptiveRateLimiter("binance"),
        mode="demo",
    )

    replacement_stop_id: str | None = None
    try:
        await exchange.load_markets()
        await _normalize_symbol_state(adapter)
        assert await adapter.get_position(SYMBOL) is None
        entry_quantity = await _entry_quantity(exchange)

        await adapter.place_entry_order(
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=entry_quantity,
            client_order_id=_client_order_id("smoke-entry"),
        )

        position = await _wait_for_position(adapter)
        assert position.side == PositionSide.LONG
        assert position.size > 0

        initial_stop = await adapter.place_stop_loss(
            symbol=SYMBOL,
            side=OrderSide.SELL,
            quantity=position.size,
            trigger_price=_sl_price(position, 0.95),
            client_order_id=_client_order_id("smoke-sl"),
        )

        visible_initial_stop = await _wait_for_stop_order(
            adapter,
            expected_order_id=initial_stop.exchange_order_id,
        )
        assert visible_initial_stop.order_type == "stop_loss"

        replacement_stop = await adapter.cancel_and_replace_sl(
            symbol=SYMBOL,
            existing_order_id=initial_stop.exchange_order_id,
            new_trigger_price=_sl_price(position, 0.94),
            new_quantity=position.size,
            client_order_id=_client_order_id("smoke-sl-repl"),
        )
        replacement_stop_id = replacement_stop.exchange_order_id

        visible_replacement_stop = await _wait_for_stop_order(
            adapter,
            expected_order_id=replacement_stop.exchange_order_id,
            absent_order_id=initial_stop.exchange_order_id,
        )
        assert visible_replacement_stop.order_type == "stop_loss"
        assert visible_replacement_stop.exchange_order_id != initial_stop.exchange_order_id

        open_orders_after_replace = await adapter.get_open_conditional_orders(SYMBOL)
        open_order_ids_after_replace = {
            order.exchange_order_id for order in open_orders_after_replace
        }
        assert replacement_stop.exchange_order_id in open_order_ids_after_replace
        assert initial_stop.exchange_order_id not in open_order_ids_after_replace

        close_result = await adapter.partial_close(
            symbol=SYMBOL,
            side=OrderSide.SELL,
            quantity=position.size,
            client_order_id=_client_order_id("smoke-close"),
        )
        assert close_result.executed_qty > 0

        await _wait_for_position_closed(adapter)
    finally:
        if replacement_stop_id:
            with contextlib.suppress(Exception):
                await adapter.cancel_conditional_order(SYMBOL, replacement_stop_id)
        with contextlib.suppress(Exception):
            await _cleanup_position(adapter)
        await exchange.close()
