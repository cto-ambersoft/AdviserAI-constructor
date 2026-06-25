"""Initial Binance SL must use ``closePosition: true`` for auto qty-tracking.

Without this flag the SL carries a fixed ``quantity`` that goes stale every
time a multi-TP level fills. With it, Binance computes the close size at
trigger time, so the SL automatically covers the remaining position after
each partial close — and we only have to re-issue it when ``sl_lock_pct``
moves the trigger price.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

from aioresponses import aioresponses

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import OrderSide  # noqa: E402
from app.services.exchange.binance_adapter import BinanceAdapter  # noqa: E402

ALGO_ORDER_URL = re.compile(r"^https://fapi\.binance\.com/fapi/v1/algoOrder\?.*$")


def _build_adapter() -> BinanceAdapter:
    exchange = MagicMock()
    exchange.options = {"enableDemoTrading": False}
    exchange.isSandboxModeEnabled = False
    exchange.urls = {
        "api": {"private": "https://fapi.binance.com", "public": "https://fapi.binance.com"}
    }
    exchange.markets = {"BTC/USDT:USDT": {"precision": {"amount": 3, "price": 1}}}
    exchange.load_markets = AsyncMock(return_value=exchange.markets)
    exchange.amount_to_precision = lambda _s, q: format(
        float(int(float(q) * 1000)) / 1000.0, ".3f"
    )
    exchange.price_to_precision = lambda _s, p: format(round(float(p), 1), ".1f")
    return BinanceAdapter(
        ccxt_exchange=exchange,
        api_key="k",
        api_secret="s",
        rate_limiter=MagicMock(),
        mode="real",
    )


def _last_post_query_params(mocked: aioresponses) -> dict[str, list[str]]:
    """Decode the most recent POST /fapi/v1/algoOrder query string."""
    for (method, url), calls in mocked.requests.items():
        if method != "POST" or "/fapi/v1/algoOrder" not in str(url):
            continue
        last_url = str(calls[-1].args[1] if len(calls[-1].args) > 1 else url)
        return parse_qs(urlsplit(last_url).query)
    raise AssertionError("No POST /fapi/v1/algoOrder request captured")


async def test_place_stop_loss_with_close_position_omits_quantity_and_reduce_only() -> None:
    adapter = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "sl-cp-1", "clientAlgoId": "cid-cp", "algoStatus": "NEW"},
        )

        result = await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.1,  # ignored when close_position=True
            trigger_price=68_000.0,
            client_order_id="cid-cp",
            close_position=True,
        )

        body = _last_post_query_params(mocked)

    assert result.exchange_order_id == "sl-cp-1"
    assert body["closePosition"] == ["true"]
    assert body["type"] == ["STOP_MARKET"]
    assert body["stopPrice"] == ["68000.0"]
    # Mutually exclusive with quantity / reduceOnly: Binance rejects all-of-them
    assert "quantity" not in body
    assert "reduceOnly" not in body


async def test_place_stop_loss_default_still_uses_quantity_and_reduce_only() -> None:
    adapter = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "sl-q-1", "clientAlgoId": "cid-q", "algoStatus": "NEW"},
        )

        result = await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.1,
            trigger_price=68_000.0,
            client_order_id="cid-q",
            # close_position not passed → defaults to False
        )

        body = _last_post_query_params(mocked)

    assert result.exchange_order_id == "sl-q-1"
    assert body["quantity"] == ["0.100"]
    assert body["reduceOnly"] == ["true"]
    assert "closePosition" not in body


async def test_place_take_profit_does_not_accept_close_position_flag() -> None:
    """TPs must remain quantity-based — multi-TP needs partial closes."""
    import inspect

    adapter = _build_adapter()
    sig = inspect.signature(adapter.place_take_profit)
    assert "close_position" not in sig.parameters, (
        "TP placement must stay quantity-based; closePosition would close the "
        "whole position on the first TP and skip the remaining levels."
    )


async def test_place_stop_loss_close_position_payload_passes_through_to_signed_request(
) -> None:
    """Sanity: full request body sent for a close_position SL is well-formed."""
    adapter = _build_adapter()

    captured: dict[str, Any] = {}

    async def _capture_signed(method: str, path: str, params: Any = None, **_kw: Any) -> Any:
        captured["method"] = method
        captured["path"] = path
        captured["params"] = dict(params) if params else {}
        return {"algoId": "captured", "clientAlgoId": "cid", "algoStatus": "NEW"}

    adapter._signed_request = _capture_signed  # type: ignore[method-assign]

    await adapter.place_stop_loss(
        symbol="BTC/USDT:USDT",
        side=OrderSide.BUY,  # SHORT close
        quantity=0.0,
        trigger_price=72_500.0,
        client_order_id="cid-short-sl",
        close_position=True,
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/fapi/v1/algoOrder"
    sent = captured["params"]
    assert sent["closePosition"] is True
    assert sent["type"] == "STOP_MARKET"
    assert sent["side"] == "BUY"
    assert sent.get("triggerPrice") == "72500.0"
    assert sent.get("stopPrice") == "72500.0"
    assert "quantity" not in sent
    assert "reduceOnly" not in sent
