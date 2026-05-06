"""Manual smoke test for Bybit Unified Trading demo trading via the custom BybitAdapter.

Required environment:
- BYBIT_DEMO_API_KEY
- BYBIT_DEMO_API_SECRET

Notes:
- Manual-only: this test is skipped in CI.
- Assumes one-way mode for the target linear contract (`positionIdx=0`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
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
from app.services.exchange.bybit_adapter import BybitAdapter, BybitAPIError  # noqa: E402
from app.services.exchange.rate_limiter import AdaptiveRateLimiter  # noqa: E402

pytestmark = pytest.mark.exchange_demo

SYMBOL = "BTC/USDT:USDT"
POSITION_TIMEOUT_SECONDS = 45.0
ORDER_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 1.0
MIN_NOTIONAL_BUFFER = Decimal("1.10")
PRICE_TOLERANCE = 1.0


def _client_order_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}"[:36]


def _configure_demo_exchange(exchange: Any) -> None:
    try:
        exchange.enable_demo_trading(True)
    except Exception as exc:  # pragma: no cover - defensive guard for manual test infra
        pytest.fail(f"Unable to configure Bybit demo trading mode: {exc}")


def _close_side(position: PositionSnapshot) -> OrderSide:
    return OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY


def _market_lot_size_filter(exchange: Any) -> dict[str, Any]:
    market = exchange.market(SYMBOL)
    info = market.get("info", {})
    if not isinstance(info, dict):
        return {}
    lot_size = info.get("lotSizeFilter", {})
    return lot_size if isinstance(lot_size, dict) else {}


def _decimal(value: Any, default: str) -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _amount_constraints(exchange: Any) -> tuple[Decimal, Decimal, Decimal]:
    market = exchange.market(SYMBOL)
    lot_size = _market_lot_size_filter(exchange)
    limits = market.get("limits", {})
    cost_limits = limits.get("cost", {}) if isinstance(limits, dict) else {}
    amount_limits = limits.get("amount", {}) if isinstance(limits, dict) else {}

    min_cost = _decimal(
        lot_size.get("minNotionalValue")
        or (cost_limits.get("min") if isinstance(cost_limits, dict) else None),
        "100",
    )
    min_amount = _decimal(
        lot_size.get("minOrderQty")
        or (amount_limits.get("min") if isinstance(amount_limits, dict) else None),
        "0.001",
    )
    step_size = _decimal(lot_size.get("qtyStep"), str(min_amount))
    if step_size <= 0:
        step_size = min_amount
    return min_cost, min_amount, step_size


def _price(exchange: Any, raw_price: float) -> float:
    return float(exchange.price_to_precision(SYMBOL, raw_price))


def _sl_price(exchange: Any, position: PositionSnapshot, multiplier: float) -> float:
    basis = position.entry_price or position.mark_price
    if basis <= 0:
        pytest.fail(f"Unable to derive stop-loss price from position snapshot: {position}")
    return _price(exchange, basis * multiplier)


def _tp_price(exchange: Any, position: PositionSnapshot, multiplier: float) -> float:
    basis = position.entry_price or position.mark_price
    if basis <= 0:
        pytest.fail(f"Unable to derive take-profit price from position snapshot: {position}")
    return _price(exchange, basis * multiplier)


def _price_matches(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= PRICE_TOLERANCE


async def _entry_quantity(exchange: Any) -> float:
    min_cost, min_amount, step_size = _amount_constraints(exchange)
    ticker = await exchange.fetch_ticker(SYMBOL)
    last_price = Decimal(
        str(
            ticker.get("last") or ticker.get("close") or ticker.get("bid") or ticker.get("ask") or 0
        )
    )
    if last_price <= 0:
        pytest.fail(f"Unable to derive {SYMBOL} price for entry sizing: {ticker}")

    raw_quantity = (min_cost * MIN_NOTIONAL_BUFFER) / last_price
    target_quantity = max(raw_quantity, min_amount * Decimal("2"))
    stepped_quantity = (
        (target_quantity / step_size).to_integral_value(rounding=ROUND_UP)
    ) * step_size
    quantity = max(min_amount, stepped_quantity)
    return float(exchange.amount_to_precision(SYMBOL, float(quantity)))


def _partial_tp_quantity(exchange: Any, position: PositionSnapshot) -> float:
    _, min_amount, step_size = _amount_constraints(exchange)
    position_size = Decimal(str(exchange.amount_to_precision(SYMBOL, position.size)))
    candidate = ((position_size / Decimal("2")) / step_size).to_integral_value(
        rounding=ROUND_DOWN
    ) * step_size
    quantity = max(min_amount, candidate)
    if quantity >= position_size:
        quantity = position_size - step_size
    normalized = Decimal(str(exchange.amount_to_precision(SYMBOL, float(quantity))))
    if normalized < min_amount or normalized <= 0:
        pytest.fail(
            "Unable to derive a valid partial TP quantity. "
            f"Position size={position_size}, min_amount={min_amount}, step_size={step_size}"
        )
    return float(normalized)


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


async def _wait_for_position(adapter: BybitAdapter) -> PositionSnapshot:
    async def _fetch() -> PositionSnapshot | None:
        return await adapter.get_position(SYMBOL)

    result = await _wait_for(
        f"an open {SYMBOL} position",
        _fetch,
        timeout=POSITION_TIMEOUT_SECONDS,
    )
    assert isinstance(result, PositionSnapshot)
    return result


async def _wait_for_single_stop_order(
    adapter: BybitAdapter,
    *,
    trigger_price: float,
) -> ConditionalOrderResult:
    async def _find() -> ConditionalOrderResult | None:
        stop_orders = [
            order
            for order in await adapter.get_open_conditional_orders(SYMBOL)
            if order.order_type == "stop_loss"
        ]
        if len(stop_orders) != 1:
            return None
        order = stop_orders[0]
        return order if _price_matches(order.trigger_price, trigger_price) else None

    result = await _wait_for(
        f"single stop-loss order at {trigger_price}",
        _find,
        timeout=ORDER_TIMEOUT_SECONDS,
    )
    assert isinstance(result, ConditionalOrderResult)
    return result


async def _wait_for_take_profit_order(
    adapter: BybitAdapter,
    *,
    trigger_price: float,
) -> ConditionalOrderResult:
    async def _find() -> ConditionalOrderResult | None:
        take_profit_orders = [
            order
            for order in await adapter.get_open_conditional_orders(SYMBOL)
            if order.order_type == "take_profit"
        ]
        if len(take_profit_orders) != 1:
            return None
        order = take_profit_orders[0]
        return order if _price_matches(order.trigger_price, trigger_price) else None

    result = await _wait_for(
        f"partial take-profit order at {trigger_price}",
        _find,
        timeout=ORDER_TIMEOUT_SECONDS,
    )
    assert isinstance(result, ConditionalOrderResult)
    return result


async def _wait_for_position_closed(adapter: BybitAdapter) -> None:
    async def _is_closed() -> bool:
        return await adapter.get_position(SYMBOL) is None

    await _wait_for(
        f"{SYMBOL} position to close",
        _is_closed,
        timeout=POSITION_TIMEOUT_SECONDS,
    )


async def _wait_for_no_open_orders(adapter: BybitAdapter) -> None:
    deadline = asyncio.get_running_loop().time() + ORDER_TIMEOUT_SECONDS
    last_orders: list[ConditionalOrderResult] = []
    while True:
        last_orders = await adapter.get_open_conditional_orders(SYMBOL)
        if not last_orders:
            return
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(
                f"Timed out waiting for no open conditional orders for {SYMBOL}. "
                f"Remaining orders: {last_orders!r}"
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _cancel_all_conditional_orders(adapter: BybitAdapter) -> None:
    await adapter.clear_symbol_conditional_orders(SYMBOL)


async def _normalize_symbol_state(adapter: BybitAdapter) -> None:
    await _cancel_all_conditional_orders(adapter)
    position = await adapter.get_position(SYMBOL)
    if position is not None:
        await adapter.partial_close(
            symbol=SYMBOL,
            side=_close_side(position),
            quantity=position.size,
            client_order_id=_client_order_id("smoke-pre-clean-close"),
        )
        await _wait_for_position_closed(adapter)
    await _cancel_all_conditional_orders(adapter)
    await _wait_for_no_open_orders(adapter)


@pytest.mark.asyncio
async def test_bybit_demo_smoke() -> None:
    if os.getenv("CI"):
        pytest.skip("Manual-only demo smoke test is skipped in CI.")

    api_key = os.getenv("BYBIT_DEMO_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_DEMO_API_SECRET", "").strip()
    if not api_key or not api_secret:
        pytest.skip("BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required.")

    exchange = ccxt.bybit(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
    )
    _configure_demo_exchange(exchange)

    adapter = BybitAdapter(
        ccxt_exchange=exchange,
        api_key=api_key,
        api_secret=api_secret,
        rate_limiter=AdaptiveRateLimiter("bybit"),
        mode="demo",
    )

    try:
        await exchange.load_markets()
        try:
            await _normalize_symbol_state(adapter)
        except BybitAPIError as exc:
            if exc.status_code == 401:
                pytest.fail(
                    "Bybit demo authentication failed. Ensure "
                    "`BYBIT_DEMO_API_KEY`/`BYBIT_DEMO_API_SECRET` are demo keys for "
                    "`api-demo.bybit.com`."
                )
            raise
        assert await adapter.get_position(SYMBOL) is None
        assert await adapter.get_open_conditional_orders(SYMBOL) == []

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

        initial_stop_price = _sl_price(exchange, position, 0.95)
        initial_stop = await adapter.place_stop_loss(
            symbol=SYMBOL,
            side=OrderSide.SELL,
            quantity=position.size,
            trigger_price=initial_stop_price,
            client_order_id=_client_order_id("smoke-sl"),
        )
        assert initial_stop.order_type == "stop_loss"

        visible_initial_stop = await _wait_for_single_stop_order(
            adapter,
            trigger_price=initial_stop_price,
        )
        assert visible_initial_stop.order_type == "stop_loss"

        replacement_stop_price = _sl_price(exchange, position, 0.94)
        replacement_stop = await adapter.cancel_and_replace_sl(
            symbol=SYMBOL,
            existing_order_id=visible_initial_stop.exchange_order_id,
            new_trigger_price=replacement_stop_price,
            new_quantity=position.size,
            client_order_id=_client_order_id("smoke-sl-repl"),
        )
        assert replacement_stop.order_type == "stop_loss"

        visible_replacement_stop = await _wait_for_single_stop_order(
            adapter,
            trigger_price=replacement_stop_price,
        )
        assert visible_replacement_stop.order_type == "stop_loss"
        assert _price_matches(visible_replacement_stop.trigger_price, replacement_stop_price)

        partial_tp_price = _tp_price(exchange, position, 1.05)
        partial_tp_quantity = _partial_tp_quantity(exchange, position)
        partial_tp = await adapter.place_take_profit(
            symbol=SYMBOL,
            side=OrderSide.SELL,
            quantity=partial_tp_quantity,
            trigger_price=partial_tp_price,
            client_order_id=_client_order_id("smoke-tp"),
        )
        assert partial_tp.order_type == "take_profit"

        visible_partial_tp = await _wait_for_take_profit_order(
            adapter,
            trigger_price=partial_tp_price,
        )
        assert visible_partial_tp.order_type == "take_profit"

        open_orders = await adapter.get_open_conditional_orders(SYMBOL)
        order_types = {order.order_type for order in open_orders}
        assert "stop_loss" in order_types
        assert "take_profit" in order_types

        close_result = await adapter.partial_close(
            symbol=SYMBOL,
            side=OrderSide.SELL,
            quantity=position.size,
            client_order_id=_client_order_id("smoke-close"),
        )
        # Bybit may acknowledge a market reduce-only order before REST reflects its fill.
        # Treat a non-empty order id as acceptance and wait for the position to disappear.
        assert close_result.order_id

        await _wait_for_position_closed(adapter)
        await _cancel_all_conditional_orders(adapter)
        await _wait_for_no_open_orders(adapter)
        assert await adapter.get_position(SYMBOL) is None
        assert await adapter.get_open_conditional_orders(SYMBOL) == []
    finally:
        try:
            with contextlib.suppress(BaseException):
                await _normalize_symbol_state(adapter)
        finally:
            await exchange.close()
