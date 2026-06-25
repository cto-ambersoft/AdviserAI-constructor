"""Bybit's `/v5/position/trading-stop` requests must include ``orderLinkId``.

Bybit echoes ``orderLinkId`` back as ``orderLinkId`` (normalised by our adapter
into ``client_order_id``) on subsequent WS execution events. Without it, our
WS manager can't map a TP/SL fill back to the level that triggered — and the
SL doesn't move (the bug at the heart of this fix series).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aioresponses import aioresponses

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import OrderSide  # noqa: E402
from app.services.exchange.bybit_adapter import BybitAdapter  # noqa: E402

TRADING_STOP_URL = re.compile(r"^https://api\.bybit\.com/v5/position/trading-stop$")


def _build_adapter() -> BybitAdapter:
    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(return_value=[])
    exchange.options = {"enableDemoTrading": False}
    exchange.isSandboxModeEnabled = False
    exchange.urls = {"api": {"private": "https://api.bybit.com", "public": "https://api.bybit.com"}}
    exchange.markets = {"BTC/USDT:USDT": {"precision": {"amount": 3, "price": 1}}}
    exchange.load_markets = AsyncMock(return_value=exchange.markets)
    exchange.amount_to_precision = lambda _symbol, amount: format(
        float(int(float(amount) * 1000)) / 1000.0, ".3f"
    )
    exchange.price_to_precision = lambda _symbol, price: format(round(float(price), 1), ".1f")
    return BybitAdapter(
        ccxt_exchange=exchange,
        api_key="api-key",
        api_secret="api-secret",
        rate_limiter=MagicMock(),
        mode="real",
    )


def _extract_body(mocked: aioresponses) -> dict[str, Any]:
    for (method, url), calls in mocked.requests.items():
        if method != "POST" or "/v5/position/trading-stop" not in str(url):
            continue
        raw = calls[-1].kwargs.get("data")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str) and raw:
            return json.loads(raw)
    raise AssertionError("trading-stop request not captured")


async def test_place_stop_loss_passes_order_link_id() -> None:
    adapter = _build_adapter()
    with aioresponses() as mocked:
        mocked.post(TRADING_STOP_URL, status=200, payload={"retCode": 0, "result": {}})
        await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.2,
            trigger_price=95_000.0,
            client_order_id="my-sl-coid",
        )
        body = _extract_body(mocked)
    assert body["orderLinkId"] == "my-sl-coid"


async def test_place_take_profit_passes_order_link_id() -> None:
    adapter = _build_adapter()
    with aioresponses() as mocked:
        mocked.post(TRADING_STOP_URL, status=200, payload={"retCode": 0, "result": {}})
        await adapter.place_take_profit(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.125,
            trigger_price=106_500.0,
            client_order_id="my-tp-coid",
        )
        body = _extract_body(mocked)
    assert body["orderLinkId"] == "my-tp-coid"


async def test_cancel_and_replace_sl_default_uses_full_mode_close_position() -> None:
    """Default ``close_position=True`` matches the multi-TP path.

    Bybit's ``Full`` mode covers the entire live position at trigger time,
    mirroring Binance ``closePosition=true`` semantics. ``slSize`` must be
    omitted because the SL auto-tracks position size; sending it would
    contradict the mode.
    """
    adapter = _build_adapter()
    with aioresponses() as mocked:
        mocked.post(TRADING_STOP_URL, status=200, payload={"retCode": 0, "result": {}})
        await adapter.cancel_and_replace_sl(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-sl",
            new_trigger_price=94_500.0,
            new_quantity=0.125,
            client_order_id="my-replace-coid",
        )
        body = _extract_body(mocked)
    assert body["tpslMode"] == "Full"
    assert "slSize" not in body, body
    assert body["orderLinkId"] == "my-replace-coid"


async def test_cancel_and_replace_sl_partial_mode_when_close_position_false() -> None:
    """Trailing/breakeven flows pass ``close_position=False`` for a sliced SL."""
    adapter = _build_adapter()
    with aioresponses() as mocked:
        mocked.post(TRADING_STOP_URL, status=200, payload={"retCode": 0, "result": {}})
        await adapter.cancel_and_replace_sl(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-sl",
            new_trigger_price=94_500.0,
            new_quantity=0.125,
            client_order_id="my-replace-coid",
            close_position=False,
        )
        body = _extract_body(mocked)
    assert body["tpslMode"] == "Partial"
    assert body["slSize"] == "0.125"
    assert body["orderLinkId"] == "my-replace-coid"
