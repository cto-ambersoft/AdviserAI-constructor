"""Regression: Binance cancel-and-replace must keep position protected.

The previous implementation ordered the operations as ``cancel, then place``:
if the new ``place_stop_loss`` failed (rate limit, transient API error), the
position was left **without any SL** because the old SL had already been
deleted.

The fix reorders to ``place new, then cancel old``:
- If the new SL placement fails, the original SL is still active.
- If the cancel fails, the position transiently has two reduce-only SL
  orders — which is safer than zero, since the first to trigger executes
  and the second becomes a no-op.

Also covers the previously-hardcoded ``OrderSide.SELL`` in
``cancel_and_replace_tp`` that broke replace_tp for short positions.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import OrderSide, PositionSide  # noqa: E402
from app.services.exchange.binance_adapter import (  # noqa: E402
    BinanceAdapter,
    CriticalSLPlacementError,
)

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


def _request_method_order(mocked: aioresponses, path_fragment: str) -> list[str]:
    """Return chronologically-ordered list of HTTP methods hitting that path."""
    events: list[tuple[float, str]] = []
    for (method, url), calls in mocked.requests.items():
        if path_fragment not in str(url):
            continue
        for call in calls:
            timestamp = float(getattr(call, "timestamp", 0.0) or 0.0)
            events.append((timestamp, method.upper()))
    events.sort(key=lambda item: item[0])
    return [method for _, method in events]


async def test_cancel_and_replace_sl_reduce_only_places_new_before_deleting_old() -> None:
    """Reduce-only path keeps the safe place-first / cancel-last ordering."""
    adapter = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "new-sl-1", "clientAlgoId": "cid-new", "algoStatus": "NEW"},
        )
        mocked.delete(
            ALGO_ORDER_URL,
            status=200,
            payload={"code": "200", "msg": "success", "algoId": "old-sl-1"},
        )

        result = await adapter.cancel_and_replace_sl(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-sl-1",
            new_trigger_price=94_000.0,
            new_quantity=0.05,
            client_order_id="cid-new",
            close_position=False,
        )

        ordering = _request_method_order(mocked, "/fapi/v1/algoOrder")

    assert result.exchange_order_id == "new-sl-1"
    # POST must come strictly before DELETE.
    assert ordering[0] == "POST"
    assert ordering.count("DELETE") == 1
    delete_index = ordering.index("DELETE")
    post_index = ordering.index("POST")
    assert post_index < delete_index


async def test_cancel_and_replace_sl_close_position_cancels_old_before_placing_new() -> None:
    """closePosition path must cancel-first: Binance forbids two GTE_GTC
    closePosition stops per direction (-4130), so place-first is impossible."""
    adapter = _build_adapter()

    with aioresponses() as mocked:
        mocked.delete(
            ALGO_ORDER_URL,
            status=200,
            payload={"code": "200", "msg": "success", "algoId": "old-sl-cp"},
        )
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "new-sl-cp", "clientAlgoId": "cid-cp", "algoStatus": "NEW"},
        )

        result = await adapter.cancel_and_replace_sl(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-sl-cp",
            new_trigger_price=94_000.0,
            new_quantity=0.05,
            client_order_id="cid-cp",
            close_position=True,
        )

        ordering = _request_method_order(mocked, "/fapi/v1/algoOrder")

    assert result.exchange_order_id == "new-sl-cp"
    # DELETE must come strictly before POST (cancel-first).
    assert ordering.count("DELETE") == 1
    assert ordering.count("POST") == 1
    assert ordering.index("DELETE") < ordering.index("POST")


async def test_cancel_and_replace_sl_close_position_recovers_from_4130() -> None:
    """If the up-front cancel fails and the first place hits -4130, the adapter
    cancels the conflicting order and retries to success."""
    adapter = _build_adapter()

    with patch("app.services.exchange.binance_adapter.asyncio.sleep", new=AsyncMock()):
        with aioresponses() as mocked:
            # Up-front cancel fails, in-loop cancel-retry succeeds.
            mocked.delete(
                ALGO_ORDER_URL,
                status=400,
                payload={"code": -2011, "msg": "Unknown order sent."},
            )
            mocked.delete(
                ALGO_ORDER_URL,
                status=200,
                payload={"code": "200", "msg": "success"},
            )
            # First place is rejected with -4130, retry succeeds.
            mocked.post(
                ALGO_ORDER_URL,
                status=400,
                payload={
                    "code": -4130,
                    "msg": (
                        "An open stop or take profit order with GTE and "
                        "closePosition in the direction is existing."
                    ),
                },
            )
            mocked.post(
                ALGO_ORDER_URL,
                status=200,
                payload={"algoId": "new-sl-recovered", "clientAlgoId": "cid", "algoStatus": "NEW"},
            )

            result = await adapter.cancel_and_replace_sl(
                symbol="BTC/USDT:USDT",
                existing_order_id="old-sl-4130",
                new_trigger_price=94_000.0,
                new_quantity=0.05,
                client_order_id="cid-4130",
                close_position=True,
            )

            ordering = _request_method_order(mocked, "/fapi/v1/algoOrder")

    assert result.exchange_order_id == "new-sl-recovered"
    # Two DELETEs (up-front + in-loop recovery) and two POSTs (-4130 then ok).
    assert ordering.count("DELETE") == 2
    assert ordering.count("POST") == 2


async def test_cancel_and_replace_sl_reduce_only_does_not_delete_old_when_place_fails() -> None:
    """Reduce-only path: if place_stop_loss exhausts retries, the old SL must
    remain untouched (place-first preserves protection)."""
    adapter = _build_adapter()

    with patch(
        "app.services.exchange.binance_adapter.asyncio.sleep", new=AsyncMock()
    ):
        with aioresponses() as mocked:
            mocked.post(
                ALGO_ORDER_URL,
                status=500,
                payload={"code": -1000, "msg": "transient"},
                repeat=True,
            )
            # No DELETE mock: any DELETE attempt would 404 and aioresponses
            # would report it. We assert the count below.

            with pytest.raises(CriticalSLPlacementError):
                await adapter.cancel_and_replace_sl(
                    symbol="BTC/USDT:USDT",
                    existing_order_id="old-sl-2",
                    new_trigger_price=93_000.0,
                    new_quantity=0.05,
                    client_order_id="cid-fail",
                    close_position=False,
                )

            ordering = _request_method_order(mocked, "/fapi/v1/algoOrder")

    # 4 POST attempts, NO DELETE — old SL is preserved.
    assert ordering.count("POST") == 4
    assert "DELETE" not in ordering


async def test_cancel_and_replace_sl_succeeds_even_if_old_cancel_fails() -> None:
    """Best-effort cancel: if DELETE fails, we still return the new SL."""
    adapter = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "new-sl-3", "clientAlgoId": "cid-3", "algoStatus": "NEW"},
        )
        mocked.delete(
            ALGO_ORDER_URL,
            status=400,
            payload={"code": -2011, "msg": "Unknown order"},
        )

        result = await adapter.cancel_and_replace_sl(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-sl-3",
            new_trigger_price=92_000.0,
            new_quantity=0.05,
            client_order_id="cid-3",
        )

    assert result.exchange_order_id == "new-sl-3"


async def test_cancel_and_replace_sl_skips_delete_for_synthetic_id() -> None:
    """Multi-TP enqueues replace_sl with ``existing_order_id="active-sl"`` placeholder.

    That string is not a real Binance algoId. Calling DELETE with it would
    400 and add noise to logs; skip it.
    """
    adapter = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "new-sl-4", "clientAlgoId": "cid-4", "algoStatus": "NEW"},
        )

        result = await adapter.cancel_and_replace_sl(
            symbol="BTC/USDT:USDT",
            existing_order_id="active-sl",  # synthetic placeholder
            new_trigger_price=91_000.0,
            new_quantity=0.05,
            client_order_id="cid-4",
        )

        ordering = _request_method_order(mocked, "/fapi/v1/algoOrder")

    assert result.exchange_order_id == "new-sl-4"
    assert "DELETE" not in ordering


async def test_cancel_and_replace_tp_uses_buy_side_for_short_position() -> None:
    """SHORT closes via BUY. Hardcoded SELL silently broke replace_tp."""
    adapter = _build_adapter()
    # Pretend the position is SHORT.
    snapshot = MagicMock()
    snapshot.side = PositionSide.SHORT
    snapshot.size = 0.05
    snapshot.entry_price = 70000.0
    snapshot.mark_price = 69500.0
    adapter.get_position = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]

    captured: dict[str, Any] = {}

    async def _capture_place_take_profit(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(
            exchange_order_id="new-tp-1",
            client_order_id=kwargs["client_order_id"],
            order_type="take_profit",
            trigger_price=kwargs["trigger_price"],
            quantity=kwargs["quantity"],
            status="new",
            is_algo=True,
        )

    adapter.place_take_profit = _capture_place_take_profit  # type: ignore[method-assign]

    with aioresponses() as mocked:
        mocked.delete(
            ALGO_ORDER_URL,
            status=200,
            payload={"code": "200", "algoId": "old-tp-1"},
        )

        await adapter.cancel_and_replace_tp(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-tp-1",
            new_trigger_price=68_500.0,
            new_quantity=0.05,
            client_order_id="cid-tp-replace",
        )

    assert captured["side"] == OrderSide.BUY


def _post_query_params(mocked: aioresponses) -> list[dict[str, str]]:
    """Return parsed query-string params for each POST /fapi/v1/algoOrder, in order."""
    from urllib.parse import parse_qs, urlsplit

    events: list[tuple[float, dict[str, str]]] = []
    for (method, url), calls in mocked.requests.items():
        if method.upper() != "POST" or "/fapi/v1/algoOrder" not in str(url):
            continue
        for call in calls:
            timestamp = float(getattr(call, "timestamp", 0.0) or 0.0)
            parsed = urlsplit(str(url))
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            events.append((timestamp, params))
    events.sort(key=lambda item: item[0])
    return [params for _, params in events]


async def test_cancel_and_replace_sl_close_position_true_omits_quantity_and_reduce_only() -> None:
    """``close_position=True`` must produce a Binance algoOrder body with
    ``closePosition=true`` and **no** ``quantity`` / ``reduceOnly`` fields.

    Binance rejects (per ``POST /fapi/v1/algoOrder`` docs) a request that
    combines ``closePosition=true`` with ``quantity`` or ``reduceOnly`` —
    so this is both a behavioural assertion and a safety check.
    """
    adapter = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "new-sl-cp", "clientAlgoId": "cid-cp", "algoStatus": "NEW"},
        )
        mocked.delete(
            ALGO_ORDER_URL,
            status=200,
            payload={"code": "200", "msg": "success"},
        )

        await adapter.cancel_and_replace_sl(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-sl-cp",
            new_trigger_price=94_000.0,
            new_quantity=0.05,
            client_order_id="cid-cp",
            close_position=True,
        )

        posts = _post_query_params(mocked)

    assert posts, "expected at least one POST /fapi/v1/algoOrder"
    params = posts[0]
    assert str(params.get("closePosition", "")).lower() == "true"
    assert "quantity" not in params, params
    assert "reduceOnly" not in params, params


async def test_cancel_and_replace_sl_retry_uses_unique_client_id() -> None:
    """On retry the clientAlgoId must change so a duplicate-id rejection on
    the second attempt unambiguously means the first attempt landed."""
    adapter = _build_adapter()

    with patch(
        "app.services.exchange.binance_adapter.asyncio.sleep", new=AsyncMock()
    ):
        with aioresponses() as mocked:
            # First attempt fails 500, subsequent succeeds.
            mocked.post(
                ALGO_ORDER_URL,
                status=500,
                payload={"code": -1000, "msg": "transient"},
            )
            mocked.post(
                ALGO_ORDER_URL,
                status=200,
                payload={"algoId": "new-sl-rid", "clientAlgoId": "cid-rid-r1", "algoStatus": "NEW"},
            )
            mocked.delete(
                ALGO_ORDER_URL,
                status=200,
                payload={"code": "200", "msg": "success"},
            )

            await adapter.cancel_and_replace_sl(
                symbol="BTC/USDT:USDT",
                existing_order_id="old-sl-rid",
                new_trigger_price=94_000.0,
                new_quantity=0.05,
                client_order_id="cid-rid",
                close_position=False,
            )

        posts = _post_query_params(mocked)

    assert len(posts) >= 2
    first_id = posts[0].get("newClientAlgoId") or posts[0].get("clientAlgoId")
    second_id = posts[1].get("newClientAlgoId") or posts[1].get("clientAlgoId")
    assert first_id and second_id and first_id != second_id, (
        f"retry must change clientAlgoId, got first={first_id!r} second={second_id!r}"
    )
    assert second_id.endswith("-r1")


async def test_cancel_and_replace_sl_duplicate_id_treated_as_success() -> None:
    """If a retry hits a duplicate-id rejection, the adapter must look the
    pre-existing order up and return it instead of raising.
    """
    adapter = _build_adapter()

    with patch(
        "app.services.exchange.binance_adapter.asyncio.sleep", new=AsyncMock()
    ):
        with aioresponses() as mocked:
            # First POST: transient 500.
            mocked.post(
                ALGO_ORDER_URL,
                status=500,
                payload={"code": -1000, "msg": "transient"},
            )
            # Second POST: duplicate-id rejection.
            mocked.post(
                ALGO_ORDER_URL,
                status=400,
                payload={"code": -4045, "msg": "duplicate clientAlgoId"},
            )
            # GET lookup returns the previously-placed algo.
            mocked.get(
                ALGO_ORDER_URL,
                status=200,
                payload={
                    "algoId": "recovered-sl",
                    "clientAlgoId": "cid-dup-r1",
                    "algoStatus": "NEW",
                    "triggerPrice": "94000.0",
                    "quantity": "0.05",
                },
            )
            mocked.delete(
                ALGO_ORDER_URL,
                status=200,
                payload={"code": "200", "msg": "success"},
            )

            result = await adapter.cancel_and_replace_sl(
                symbol="BTC/USDT:USDT",
                existing_order_id="old-sl-dup",
                new_trigger_price=94_000.0,
                new_quantity=0.05,
                client_order_id="cid-dup",
                close_position=False,
            )

    assert result.exchange_order_id == "recovered-sl"


async def test_cancel_and_replace_tp_uses_sell_side_for_long_position() -> None:
    """Sanity: LONG still closes via SELL after the side-resolver change."""
    adapter = _build_adapter()
    snapshot = MagicMock()
    snapshot.side = PositionSide.LONG
    snapshot.size = 0.05
    adapter.get_position = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]

    captured: dict[str, Any] = {}

    async def _capture_place_take_profit(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(
            exchange_order_id="new-tp-2",
            client_order_id=kwargs["client_order_id"],
            order_type="take_profit",
            trigger_price=kwargs["trigger_price"],
            quantity=kwargs["quantity"],
            status="new",
            is_algo=True,
        )

    adapter.place_take_profit = _capture_place_take_profit  # type: ignore[method-assign]

    with aioresponses() as mocked:
        mocked.delete(
            ALGO_ORDER_URL,
            status=200,
            payload={"code": "200", "algoId": "old-tp-2"},
        )

        await adapter.cancel_and_replace_tp(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-tp-2",
            new_trigger_price=72_000.0,
            new_quantity=0.05,
            client_order_id="cid-tp-replace-2",
        )

    assert captured["side"] == OrderSide.SELL
