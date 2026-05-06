"""Unit tests for Binance Futures adapter (Algo Orders API)."""

from __future__ import annotations

import asyncio
import json
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
    BinanceAPIError,
    CriticalSLPlacementError,
)

ALGO_ORDER_URL = re.compile(r"^https://fapi\.binance\.com/fapi/v1/algoOrder\?.*$")
OPEN_ALGO_ORDERS_URL = re.compile(r"^https://fapi\.binance\.com/fapi/v1/openAlgoOrders\?.*$")
OLD_ORDER_URL = re.compile(r"^https://fapi\.binance\.com/fapi/v1/order\?.*$")


class _FakeWebSocket:
    def __init__(
        self,
        messages: list[str] | None = None,
        *,
        iteration_error: Exception | None = None,
    ) -> None:
        self._messages = list(messages or [])
        self._iteration_error = iteration_error

    async def __aenter__(self) -> _FakeWebSocket:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        if self._iteration_error is not None:
            raise self._iteration_error
        raise StopAsyncIteration


def _build_adapter(
    *,
    ccxt_exchange: Any | None = None,
    rate_limiter: MagicMock | None = None,
    mode: str = "real",
) -> tuple[BinanceAdapter, Any, MagicMock]:
    exchange = ccxt_exchange if ccxt_exchange is not None else MagicMock()
    exchange.options = {"enableDemoTrading": mode == "demo"}
    exchange.isSandboxModeEnabled = False
    exchange.urls = {
        "api": {
            "private": (
                "https://demo-fapi.binance.com" if mode == "demo" else "https://fapi.binance.com"
            ),
            "public": (
                "https://demo-fapi.binance.com" if mode == "demo" else "https://fapi.binance.com"
            ),
        }
    }
    # Pretend markets are already loaded so the adapter skips load_markets().
    if not getattr(exchange, "markets", None):
        exchange.markets = {"BTC/USDT:USDT": {"precision": {"amount": 3, "price": 1}}}
    if not isinstance(getattr(exchange, "load_markets", None), AsyncMock):
        exchange.load_markets = AsyncMock(return_value=exchange.markets)
    # CCXT precision helpers are sync; mimic Binance BTC-USDT-perp filters by default.
    if not callable(getattr(exchange, "amount_to_precision", None)) or isinstance(
        exchange.amount_to_precision, MagicMock
    ):
        exchange.amount_to_precision = lambda symbol, amount: format(
            float(int(float(amount) * 1000)) / 1000.0, ".3f"
        )
    if not callable(getattr(exchange, "price_to_precision", None)) or isinstance(
        exchange.price_to_precision, MagicMock
    ):
        exchange.price_to_precision = lambda symbol, price: format(
            round(float(price), 1), ".1f"
        )
    limiter = rate_limiter if rate_limiter is not None else MagicMock()
    adapter = BinanceAdapter(
        ccxt_exchange=exchange,
        api_key="api-key",
        api_secret="api-secret",
        rate_limiter=limiter,
        mode=mode,
    )
    return adapter, exchange, limiter


def _count_requests(mocked: aioresponses, method: str, path_fragment: str) -> int:
    total = 0
    for (request_method, request_url), calls in mocked.requests.items():
        if request_method != method.upper():
            continue
        if path_fragment in str(request_url):
            total += len(calls)
    return total


def _algo_order_call_bodies(mocked: aioresponses) -> list[dict[str, Any]]:
    """Extract decoded query params (sorted by timestamp) for /fapi/v1/algoOrder POSTs."""
    from urllib.parse import parse_qs, urlsplit

    bodies: list[tuple[float, dict[str, Any]]] = []
    for (request_method, request_url), calls in mocked.requests.items():
        if request_method != "POST" or "/fapi/v1/algoOrder" not in str(request_url):
            continue
        query = urlsplit(str(request_url)).query
        if not query:
            continue
        parsed = {key: values[0] for key, values in parse_qs(query).items()}
        timestamp = float(parsed.get("timestamp", "0") or 0.0)
        for _ in calls:
            bodies.append((timestamp, parsed))
    bodies.sort(key=lambda item: item[0])
    return [body for _, body in bodies]


async def test_place_stop_loss_returns_algo_id_and_algo_flag() -> None:
    adapter, _, _ = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "algo-101", "clientAlgoId": "cid-101", "algoStatus": "NEW"},
        )
        result = await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.01,
            trigger_price=95_000.0,
            client_order_id="cid-101",
        )

    assert result.exchange_order_id == "algo-101"
    assert result.is_algo is True
    assert _count_requests(mocked, "POST", "/fapi/v1/algoOrder") == 1


def test_demo_adapter_uses_demo_private_and_mainnet_market_ws_endpoints() -> None:
    adapter, _, _ = _build_adapter(mode="demo")

    assert adapter._base_url == "https://demo-fapi.binance.com"
    assert adapter._user_data_ws_base_url == "wss://demo-fstream.binance.com/ws"
    assert adapter._market_ws_base_url == "wss://fstream.binance.com/ws"


def test_demo_adapter_rejects_sandbox_configured_ccxt_exchange() -> None:
    exchange = MagicMock()
    exchange.options = {"enableDemoTrading": False}
    exchange.isSandboxModeEnabled = True
    exchange.urls = {
        "api": {
            "private": "https://testnet.binancefuture.com",
            "public": "https://testnet.binancefuture.com",
        }
    }

    with pytest.raises(ValueError, match="sandbox endpoints"):
        BinanceAdapter(
            ccxt_exchange=exchange,
            api_key="api-key",
            api_secret="api-secret",
            rate_limiter=MagicMock(),
            mode="demo",
        )


async def test_place_stop_loss_updates_rate_limiter_from_response_headers() -> None:
    limiter = MagicMock()
    adapter, _, _ = _build_adapter(rate_limiter=limiter)

    headers = {
        "X-MBX-ORDER-COUNT-10S": "21",
        "X-MBX-ORDER-COUNT-1M": "111",
        "X-MBX-USED-WEIGHT-1M": "300",
    }

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "algo-102", "clientAlgoId": "cid-102", "algoStatus": "NEW"},
            headers=headers,
        )
        await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.01,
            trigger_price=95_000.0,
            client_order_id="cid-102",
        )

    limiter.update_from_headers.assert_called_once()
    called_headers = limiter.update_from_headers.call_args.args[0]
    normalized = {str(key).lower(): str(value) for key, value in called_headers.items()}
    assert normalized["x-mbx-order-count-10s"] == "21"
    assert normalized["x-mbx-order-count-1m"] == "111"
    assert normalized["x-mbx-used-weight-1m"] == "300"


async def test_get_open_conditional_orders_uses_open_algo_orders_endpoint() -> None:
    adapter, _, _ = _build_adapter()

    with aioresponses() as mocked:
        mocked.get(
            OPEN_ALGO_ORDERS_URL,
            status=200,
            payload=[
                {
                    "algoId": "algo-open-1",
                    "clientAlgoId": "cid-open-1",
                    "type": "STOP_MARKET",
                    "triggerPrice": "95000",
                    "quantity": "0.01",
                    "algoStatus": "NEW",
                }
            ],
        )
        orders = await adapter.get_open_conditional_orders("BTC/USDT:USDT")

    assert len(orders) == 1
    assert orders[0].exchange_order_id == "algo-open-1"
    assert orders[0].order_type == "stop_loss"
    assert _count_requests(mocked, "GET", "/fapi/v1/openAlgoOrders") == 1


async def test_cancel_and_replace_sl_happy_path() -> None:
    adapter, _, _ = _build_adapter()

    with patch(
        "app.services.exchange.binance_adapter.asyncio.sleep",
        new=AsyncMock(),
    ) as sleep_mock:
        with aioresponses() as mocked:
            mocked.delete(ALGO_ORDER_URL, status=200, payload={"code": "200", "algoId": "old-1"})
            mocked.post(
                ALGO_ORDER_URL,
                status=200,
                payload={"algoId": "new-1", "clientAlgoId": "cid-new", "algoStatus": "NEW"},
            )

            result = await adapter.cancel_and_replace_sl(
                symbol="BTC/USDT:USDT",
                existing_order_id="old-1",
                new_trigger_price=94_000.0,
                new_quantity=0.02,
                client_order_id="cid-new",
            )

    assert result.exchange_order_id == "new-1"
    assert _count_requests(mocked, "DELETE", "/fapi/v1/algoOrder") == 1
    assert _count_requests(mocked, "POST", "/fapi/v1/algoOrder") == 1
    sleep_mock.assert_not_called()


async def test_cancel_and_replace_sl_retries_then_succeeds() -> None:
    adapter, _, _ = _build_adapter()

    with patch(
        "app.services.exchange.binance_adapter.asyncio.sleep",
        new=AsyncMock(),
    ) as sleep_mock:
        with aioresponses() as mocked:
            mocked.delete(ALGO_ORDER_URL, status=200, payload={"code": "200", "algoId": "old-2"})
            mocked.post(
                ALGO_ORDER_URL,
                status=500,
                payload={"code": -1000, "msg": "temporary error"},
                repeat=3,
            )
            mocked.post(
                ALGO_ORDER_URL,
                status=200,
                payload={"algoId": "new-2", "clientAlgoId": "cid-new-2", "algoStatus": "NEW"},
            )

            result = await adapter.cancel_and_replace_sl(
                symbol="BTC/USDT:USDT",
                existing_order_id="old-2",
                new_trigger_price=93_500.0,
                new_quantity=0.02,
                client_order_id="cid-new-2",
            )

    assert result.exchange_order_id == "new-2"
    assert _count_requests(mocked, "POST", "/fapi/v1/algoOrder") == 4
    assert sleep_mock.await_count == 3


async def test_cancel_and_replace_sl_raises_when_all_retries_failed() -> None:
    adapter, _, _ = _build_adapter()

    with patch(
        "app.services.exchange.binance_adapter.asyncio.sleep",
        new=AsyncMock(),
    ) as sleep_mock:
        with aioresponses() as mocked:
            mocked.delete(ALGO_ORDER_URL, status=200, payload={"code": "200", "algoId": "old-3"})
            mocked.post(
                ALGO_ORDER_URL,
                status=500,
                payload={"code": -1000, "msg": "temporary error"},
                repeat=True,
            )

            with pytest.raises(CriticalSLPlacementError):
                await adapter.cancel_and_replace_sl(
                    symbol="BTC/USDT:USDT",
                    existing_order_id="old-3",
                    new_trigger_price=93_000.0,
                    new_quantity=0.02,
                    client_order_id="cid-new-3",
                )

    assert _count_requests(mocked, "POST", "/fapi/v1/algoOrder") == 4
    assert sleep_mock.await_count == 3


async def test_get_position_maps_fetch_positions_to_position_snapshot() -> None:
    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC/USDT:USDT",
                "contracts": "0.7",
                "side": "long",
                "entryPrice": "101000.5",
                "unrealizedPnl": "55.3",
                "leverage": "12",
                "markPrice": "101120.2",
                "liquidationPrice": "80200.1",
            }
        ]
    )
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    position = await adapter.get_position("BTC/USDT:USDT")

    assert position is not None
    assert position.side == PositionSide.LONG
    assert position.size == pytest.approx(0.7)
    assert position.entry_price == pytest.approx(101000.5)
    assert position.unrealized_pnl == pytest.approx(55.3)
    assert position.leverage == 12
    assert position.mark_price == pytest.approx(101120.2)
    assert position.liquidation_price == pytest.approx(80200.1)
    assert position.open_orders == []


async def test_partial_close_uses_ccxt_reduce_only_order() -> None:
    exchange = MagicMock()
    exchange.create_order = AsyncMock(
        return_value={
            "id": "close-1",
            "filled": 0.3,
            "average": 101500.0,
            "remaining": 0.2,
            "fee": {"cost": 0.03},
        }
    )
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    result = await adapter.partial_close(
        symbol="BTC/USDT:USDT",
        side=OrderSide.SELL,
        quantity=0.5,
        client_order_id="close-cid-1",
    )

    exchange.create_order.assert_awaited_once()
    _, kwargs = exchange.create_order.call_args
    assert kwargs["params"]["reduceOnly"] is True
    assert kwargs["params"]["newClientOrderId"] == "close-cid-1"

    assert result.order_id == "close-1"
    assert result.executed_qty == pytest.approx(0.3)
    assert result.remaining_qty == pytest.approx(0.2)
    assert result.avg_price == pytest.approx(101500.0)
    assert result.commission == pytest.approx(0.03)


async def test_stop_loss_never_uses_legacy_order_endpoint_with_4120_error() -> None:
    adapter, _, _ = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            OLD_ORDER_URL,
            status=400,
            payload={"code": -4120, "msg": "STOP_ORDER_SWITCH_ALGO"},
        )
        mocked.post(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "algo-200", "clientAlgoId": "cid-200", "algoStatus": "NEW"},
        )

        result = await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.01,
            trigger_price=95_000.0,
            client_order_id="cid-200",
        )

    assert result.exchange_order_id == "algo-200"
    assert _count_requests(mocked, "POST", "/fapi/v1/order") == 0
    assert _count_requests(mocked, "POST", "/fapi/v1/algoOrder") == 1


async def test_subscribe_user_data_routes_order_algo_and_account_events(monkeypatch) -> None:
    adapter, _, _ = _build_adapter()
    adapter._signed_request = AsyncMock(return_value={"listenKey": "listen-key-1"})  # type: ignore[method-assign]

    connect_mock = MagicMock(
        return_value=_FakeWebSocket(
            messages=[
                json.dumps(
                    {
                        "e": "ORDER_TRADE_UPDATE",
                        "E": 1001,
                        "T": 1000,
                        "o": {
                            "s": "BTCUSDT",
                            "c": "cid-1",
                            "S": "SELL",
                            "o": "STOP_MARKET",
                            "q": "0.01",
                            "sp": "95000",
                            "x": "NEW",
                            "X": "NEW",
                            "i": 111,
                            "ps": "LONG",
                        },
                    }
                ),
                json.dumps(
                    {
                        "e": "ALGO_UPDATE",
                        "E": 1002,
                        "T": 1001,
                        "ao": {
                            "s": "BTCUSDT",
                            "c": "algo-1",
                            "S": "SELL",
                            "o": "TAKE_PROFIT_MARKET",
                            "q": "0.02",
                            "sp": "101500",
                            "X": "TRIGGERED",
                            "i": 222,
                            "ps": "LONG",
                        },
                    }
                ),
                json.dumps(
                    {
                        "e": "ACCOUNT_UPDATE",
                        "E": 1003,
                        "T": 1002,
                        "a": {
                            "m": "ORDER",
                            "P": [
                                {
                                    "s": "BTCUSDT",
                                    "pa": "0.02",
                                    "ep": "100000",
                                    "bep": "100010",
                                    "up": "12.5",
                                    "mt": "isolated",
                                    "iw": "100.0",
                                    "ps": "LONG",
                                }
                            ],
                        },
                    }
                ),
            ]
        )
    )
    monkeypatch.setattr("app.services.exchange.binance_adapter.websockets.connect", connect_mock)

    on_order_update = AsyncMock()
    on_position_update = AsyncMock()
    on_disconnect = AsyncMock()

    await adapter.subscribe_user_data(on_order_update, on_position_update, on_disconnect)
    assert adapter._user_data_task is not None
    await asyncio.wait_for(adapter._user_data_task, timeout=1.0)

    adapter._signed_request.assert_awaited_once_with("POST", "/fapi/v1/listenKey", signed=False)
    connect_mock.assert_called_once()
    connect_uri = connect_mock.call_args.args[0]
    assert connect_uri == "wss://fstream.binance.com/ws/listen-key-1"

    assert on_order_update.await_count == 2
    first_order = on_order_update.await_args_list[0].args[0]
    second_order = on_order_update.await_args_list[1].args[0]
    position_event = on_position_update.await_args.args[0]

    assert first_order["type"] == "ORDER_TRADE_UPDATE"
    assert first_order["symbol"] == "BTCUSDT"
    assert first_order["order_id"] == "111"
    assert first_order["status"] == "new"
    assert first_order["price"] == pytest.approx(95000.0)
    assert first_order["quantity"] == pytest.approx(0.01)
    assert first_order["side"] == "sell"
    assert first_order["is_algo"] is False

    assert second_order["type"] == "ALGO_UPDATE"
    assert second_order["order_type"] == "take_profit"
    assert second_order["status"] == "triggered"
    assert second_order["is_algo"] is True

    assert position_event["type"] == "ACCOUNT_UPDATE"
    assert position_event["symbol"] == "BTCUSDT"
    assert position_event["size"] == pytest.approx(0.02)
    assert position_event["reason"] == "ORDER"
    on_disconnect.assert_awaited_once()


async def test_subscribe_user_data_calls_disconnect_on_stream_error(monkeypatch) -> None:
    adapter, _, _ = _build_adapter()
    adapter._signed_request = AsyncMock(return_value={"listenKey": "listen-key-2"})  # type: ignore[method-assign]

    monkeypatch.setattr(
        "app.services.exchange.binance_adapter.websockets.connect",
        MagicMock(return_value=_FakeWebSocket(iteration_error=RuntimeError("boom"))),
    )

    on_disconnect = AsyncMock()
    await adapter.subscribe_user_data(AsyncMock(), AsyncMock(), on_disconnect)
    assert adapter._user_data_task is not None
    await asyncio.wait_for(adapter._user_data_task, timeout=1.0)

    on_disconnect.assert_awaited_once()


async def test_keepalive_listen_key_puts_every_thirty_minutes(monkeypatch) -> None:
    adapter, _, _ = _build_adapter()
    signed_request = AsyncMock(return_value={})
    adapter._signed_request = signed_request  # type: ignore[method-assign]

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr("app.services.exchange.binance_adapter.asyncio.sleep", sleep_mock)

    with pytest.raises(asyncio.CancelledError):
        await adapter._keepalive_listen_key("listen-key-3")

    sleep_mock.assert_any_await(30 * 60)
    signed_request.assert_awaited_once_with(
        "PUT",
        "/fapi/v1/listenKey",
        {"listenKey": "listen-key-3"},
        signed=False,
    )


async def test_subscribe_kline_routes_normalized_kline_payload(monkeypatch) -> None:
    adapter, _, _ = _build_adapter()
    connect_mock = MagicMock(
        return_value=_FakeWebSocket(
            messages=[
                json.dumps(
                    {
                        "e": "kline",
                        "E": 123456789,
                        "s": "BTCUSDT",
                        "k": {
                            "t": 123400000,
                            "T": 123460000,
                            "s": "BTCUSDT",
                            "i": "1m",
                            "o": "100000",
                            "c": "100500",
                            "h": "100600",
                            "l": "99900",
                            "v": "12.5",
                            "n": 42,
                            "x": True,
                            "q": "1250000",
                            "V": "6.0",
                            "Q": "600000",
                        },
                    }
                )
            ]
        )
    )
    monkeypatch.setattr("app.services.exchange.binance_adapter.websockets.connect", connect_mock)

    on_kline = AsyncMock()
    await adapter.subscribe_kline("BTC/USDT:USDT", "1m", on_kline)

    task = adapter._kline_tasks["btcusdt@kline_1m"]
    await asyncio.wait_for(task, timeout=1.0)

    connect_uri = connect_mock.call_args.args[0]
    assert connect_uri == "wss://fstream.binance.com/ws/btcusdt@kline_1m"
    on_kline.assert_awaited_once()
    event = on_kline.await_args.args[0]
    assert event["symbol"] == "BTCUSDT"
    assert event["interval"] == "1m"
    assert event["open"] == pytest.approx(100000.0)
    assert event["close"] == pytest.approx(100500.0)
    assert event["is_closed"] is True


# ─── place_entry_order: bracket-at-entry ──────────────────────────────────────


def _entry_ccxt_payload(*, order_id: str = "entry-1", filled: float = 0.5) -> dict[str, Any]:
    return {
        "id": order_id,
        "clientOrderId": "client-entry-1",
        "symbol": "BTC/USDT:USDT",
        "type": "market",
        "status": "closed",
        "amount": filled,
        "filled": filled,
        "remaining": 0.0,
        "price": 100_000.0,
        "average": 100_000.0,
        "cost": filled * 100_000.0,
        "timestamp": 1_700_000_000_000,
    }


async def test_place_entry_order_without_bracket_skips_protective() -> None:
    exchange = MagicMock()
    exchange.create_order = AsyncMock(return_value=_entry_ccxt_payload())
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    with aioresponses() as mocked:
        result = await adapter.place_entry_order(
            symbol="BTC/USDT:USDT",
            side=OrderSide.BUY,
            quantity=0.5,
            client_order_id="client-entry-1",
        )
        # No algoOrder calls — only the CCXT entry was issued.
        assert _count_requests(mocked, "POST", "/fapi/v1/algoOrder") == 0

    exchange.create_order.assert_awaited_once()
    assert result.attached_sl is None
    assert result.attached_tp is None
    assert result.filled_quantity == pytest.approx(0.5)


async def test_place_entry_order_with_bracket_attaches_sl_and_tp() -> None:
    exchange = MagicMock()
    exchange.create_order = AsyncMock(return_value=_entry_ccxt_payload())
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    sl_payload = {
        "algoId": "sl-algo-1",
        "clientAlgoId": "client-entry-1-sl",
        "algoStatus": "NEW",
    }
    tp_payload = {
        "algoId": "tp-algo-1",
        "clientAlgoId": "client-entry-1-tp",
        "algoStatus": "NEW",
    }
    with aioresponses() as mocked:
        mocked.post(ALGO_ORDER_URL, status=200, payload=sl_payload)
        mocked.post(ALGO_ORDER_URL, status=200, payload=tp_payload)
        result = await adapter.place_entry_order(
            symbol="BTC/USDT:USDT",
            side=OrderSide.BUY,
            quantity=0.5,
            client_order_id="client-entry-1",
            stop_loss_price=95_000.0,
            take_profit_price=105_000.0,
        )

        assert _count_requests(mocked, "POST", "/fapi/v1/algoOrder") == 2
        # Verify SL went out before TP and both included reduce-only + derived client ids.
        algo_calls = _algo_order_call_bodies(mocked)
        assert [body["type"] for body in algo_calls] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
        assert all(body["reduceOnly"] in {"true", True} for body in algo_calls)
        assert algo_calls[0]["newClientAlgoId"] == "client-entry-1-sl"
        assert algo_calls[1]["newClientAlgoId"] == "client-entry-1-tp"

    assert result.attached_sl is not None
    assert result.attached_sl.exchange_order_id == "sl-algo-1"
    assert result.attached_sl.order_type == "stop_loss"
    assert result.attached_tp is not None
    assert result.attached_tp.exchange_order_id == "tp-algo-1"
    assert result.attached_tp.order_type == "take_profit"


async def test_place_entry_order_sl_failure_rolls_back_entry() -> None:
    rollback_payload = {
        "id": "rb-1",
        "filled": 0.5,
        "average": 100_000.0,
        "remaining": 0.0,
        "fee": {"cost": 0.0},
    }
    exchange = MagicMock()
    exchange.create_order = AsyncMock(
        side_effect=[_entry_ccxt_payload(), rollback_payload]
    )
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    with aioresponses() as mocked:
        mocked.post(
            ALGO_ORDER_URL,
            status=400,
            payload={"code": -2010, "msg": "Margin is insufficient."},
        )
        with pytest.raises(BinanceAPIError):
            await adapter.place_entry_order(
                symbol="BTC/USDT:USDT",
                side=OrderSide.BUY,
                quantity=0.5,
                client_order_id="client-entry-1",
                stop_loss_price=95_000.0,
                take_profit_price=105_000.0,
            )

    # Two CCXT calls: entry, then rollback partial_close.
    assert exchange.create_order.await_count == 2
    rollback_kwargs = exchange.create_order.await_args_list[1].kwargs
    assert rollback_kwargs["params"]["reduceOnly"] is True
    assert rollback_kwargs["params"]["newClientOrderId"].endswith("-rb")


async def test_place_entry_order_tp_failure_cancels_sl_and_rolls_back() -> None:
    rollback_payload = {
        "id": "rb-2",
        "filled": 0.5,
        "average": 100_000.0,
        "remaining": 0.0,
        "fee": {"cost": 0.0},
    }
    exchange = MagicMock()
    exchange.create_order = AsyncMock(
        side_effect=[_entry_ccxt_payload(), rollback_payload]
    )
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    delete_calls: list[str] = []

    def _delete_callback(url: str, **kwargs: Any) -> Any:
        delete_calls.append(str(url))
        return None

    sl_payload = {
        "algoId": "sl-algo-77",
        "clientAlgoId": "client-entry-1-sl",
        "algoStatus": "NEW",
    }
    with aioresponses() as mocked:
        mocked.post(ALGO_ORDER_URL, status=200, payload=sl_payload)
        mocked.post(ALGO_ORDER_URL, status=400, payload={"code": -1234, "msg": "TP rejected"})
        mocked.delete(
            ALGO_ORDER_URL,
            status=200,
            payload={"algoId": "sl-algo-77", "algoStatus": "CANCELED"},
            callback=_delete_callback,
        )
        with pytest.raises(BinanceAPIError):
            await adapter.place_entry_order(
                symbol="BTC/USDT:USDT",
                side=OrderSide.BUY,
                quantity=0.5,
                client_order_id="client-entry-1",
                stop_loss_price=95_000.0,
                take_profit_price=105_000.0,
            )

    # SL cancel was issued during rollback.
    assert any("algoId=sl-algo-77" in url for url in delete_calls)
    # Rollback partial_close happened.
    assert exchange.create_order.await_count == 2
    rollback_kwargs = exchange.create_order.await_args_list[1].kwargs
    assert rollback_kwargs["params"]["reduceOnly"] is True


# ─── precision: -1111 regression ──────────────────────────────────────────────


async def test_place_stop_loss_rounds_quantity_and_price_to_market_precision() -> None:
    """Regression for Binance error -1111: quantity/triggerPrice must respect step/tick."""
    adapter, _, _ = _build_adapter()
    sl_payload = {"algoId": "sl-rnd", "clientAlgoId": "cid-rnd", "algoStatus": "NEW"}
    with aioresponses() as mocked:
        mocked.post(ALGO_ORDER_URL, status=200, payload=sl_payload)
        await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            # Both values intentionally over the BTCUSDT-perp precision (3 dp / 1 dp).
            quantity=0.0015289123,
            trigger_price=64777.7790,
            client_order_id="cid-rnd",
        )

    body = _algo_order_call_bodies(mocked)[-1]
    # 3 decimal places on quantity (rounded down via amount_to_precision mock)
    assert body["quantity"] == "0.001"
    # 1 decimal place on price (rounded via price_to_precision mock)
    assert body["triggerPrice"] == "64777.8"
    assert body["stopPrice"] == "64777.8"


async def test_place_entry_order_rounds_quantity_before_create_order() -> None:
    """Entry market order amount must be precision-formatted before reaching CCXT."""
    exchange = MagicMock()
    exchange.create_order = AsyncMock(return_value=_entry_ccxt_payload())
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    await adapter.place_entry_order(
        symbol="BTC/USDT:USDT",
        side=OrderSide.BUY,
        quantity=0.0015289123,
        client_order_id="cid-rnd",
    )

    args, _kwargs = exchange.create_order.call_args
    # Positional arg index 3 is `amount`.
    assert args[3] == pytest.approx(0.001)
