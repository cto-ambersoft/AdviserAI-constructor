"""Unit tests for Bybit V5 exchange adapter."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioresponses import aioresponses

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import OrderSide, PositionSide  # noqa: E402
from app.services.exchange.bybit_adapter import BybitAdapter  # noqa: E402

TRADING_STOP_URL = re.compile(r"^https://api\.bybit\.com/v5/position/trading-stop$")


class _FakeWebSocket:
    def __init__(
        self,
        messages: list[str] | None = None,
        *,
        iteration_error: Exception | None = None,
    ) -> None:
        self._messages = list(messages or [])
        self._iteration_error = iteration_error
        self.sent_messages: list[dict[str, Any]] = []

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

    async def recv(self) -> str:
        return await self.__anext__()

    async def send(self, payload: str) -> None:
        self.sent_messages.append(json.loads(payload))


def _build_adapter(
    *,
    ccxt_exchange: Any | None = None,
    rate_limiter: MagicMock | None = None,
    mode: str = "real",
) -> tuple[BybitAdapter, Any, MagicMock]:
    exchange = ccxt_exchange if ccxt_exchange is not None else MagicMock()
    if not isinstance(getattr(exchange, "fetch_positions", None), AsyncMock):
        exchange.fetch_positions = AsyncMock(return_value=[])
    exchange.options = {"enableDemoTrading": mode == "demo"}
    exchange.isSandboxModeEnabled = False
    exchange.urls = {
        "api": {
            "private": (
                "https://api-demo.bybit.com" if mode == "demo" else "https://api.bybit.com"
            ),
            "public": ("https://api-demo.bybit.com" if mode == "demo" else "https://api.bybit.com"),
        }
    }
    if not getattr(exchange, "markets", None):
        exchange.markets = {"BTC/USDT:USDT": {"precision": {"amount": 3, "price": 1}}}
    if not isinstance(getattr(exchange, "load_markets", None), AsyncMock):
        exchange.load_markets = AsyncMock(return_value=exchange.markets)
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
    adapter = BybitAdapter(
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


def _extract_last_request_json(
    mocked: aioresponses,
    method: str,
    path_fragment: str,
) -> dict[str, Any]:
    for (request_method, request_url), calls in mocked.requests.items():
        if request_method != method.upper():
            continue
        if path_fragment not in str(request_url):
            continue
        if not calls:
            continue
        raw_data = calls[-1].kwargs.get("data")
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8")
        if isinstance(raw_data, str) and raw_data:
            return json.loads(raw_data)
    raise AssertionError(f"Request {method} {path_fragment} not found")


async def test_place_stop_loss_calls_trading_stop_with_expected_params() -> None:
    adapter, _, _ = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            TRADING_STOP_URL,
            status=200,
            payload={"retCode": 0, "retMsg": "OK", "result": {}},
        )
        await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.2,
            trigger_price=95_000.0,
            client_order_id="sl-cid-1",
        )

    body = _extract_last_request_json(mocked, "POST", "/v5/position/trading-stop")
    assert body["category"] == "linear"
    assert body["symbol"] == "BTCUSDT"
    assert body["stopLoss"] == "95000.0"
    assert body["slTriggerBy"] == "MarkPrice"
    assert body["positionIdx"] == 0
    assert _count_requests(mocked, "POST", "/v5/position/trading-stop") == 1


def test_demo_adapter_uses_demo_private_and_mainnet_public_endpoints() -> None:
    adapter, _, _ = _build_adapter(mode="demo")

    assert adapter._base_url == "https://api-demo.bybit.com"
    assert adapter._PRIVATE_WS_URL == "wss://stream-demo.bybit.com/v5/private"
    assert adapter._PUBLIC_LINEAR_WS_URL == "wss://stream.bybit.com/v5/public/linear"


def test_demo_adapter_rejects_sandbox_configured_ccxt_exchange() -> None:
    exchange = MagicMock()
    exchange.options = {"enableDemoTrading": False}
    exchange.isSandboxModeEnabled = True
    exchange.urls = {
        "api": {
            "private": "https://api-testnet.bybit.com",
            "public": "https://api-testnet.bybit.com",
        }
    }

    with pytest.raises(ValueError, match="sandbox endpoints"):
        BybitAdapter(
            ccxt_exchange=exchange,
            api_key="api-key",
            api_secret="api-secret",
            rate_limiter=MagicMock(),
            mode="demo",
        )


async def test_cancel_and_replace_sl_uses_single_atomic_trading_stop_call() -> None:
    adapter, _, _ = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            TRADING_STOP_URL,
            status=200,
            payload={"retCode": 0, "retMsg": "OK", "result": {}},
        )
        await adapter.cancel_and_replace_sl(
            symbol="BTC/USDT:USDT",
            existing_order_id="old-sl-1",
            new_trigger_price=94_500.0,
            new_quantity=0.2,
            client_order_id="sl-cid-2",
        )

    assert _count_requests(mocked, "POST", "/v5/position/trading-stop") == 1
    assert _count_requests(mocked, "POST", "/v5/order/amend") == 0
    assert _count_requests(mocked, "POST", "/v5/order/cancel") == 0


async def test_place_take_profit_partial_mode_passes_matching_tp_and_sl_sizes() -> None:
    adapter, _, _ = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            TRADING_STOP_URL,
            status=200,
            payload={"retCode": 0, "retMsg": "OK", "result": {}},
        )
        await adapter.place_take_profit(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.125,
            trigger_price=106_500.0,
            client_order_id="tp-cid-1",
        )

    body = _extract_last_request_json(mocked, "POST", "/v5/position/trading-stop")
    assert body["tpslMode"] == "Partial"
    assert body["tpSize"] == "0.125"
    assert body["slSize"] == "0.125"
    assert body["tpTriggerBy"] == "MarkPrice"
    assert body["tpOrderType"] == "Market"


async def test_get_open_conditional_orders_includes_full_position_stop_loss() -> None:
    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC/USDT:USDT",
                "contracts": "0.2",
                "side": "buy",
                "info": {
                    "size": "0.2",
                    "stopLoss": "95000",
                    "takeProfit": "0",
                    "trailingStop": "0",
                },
            }
        ]
    )
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://api\.bybit\.com/v5/order/realtime\?.*$"),
            status=200,
            payload={"retCode": 0, "retMsg": "OK", "result": {"list": []}},
        )
        orders = await adapter.get_open_conditional_orders("BTC/USDT:USDT")

    assert len(orders) == 1
    assert orders[0].exchange_order_id == "bybit-position-tpsl:stop_loss:BTCUSDT"
    assert orders[0].order_type == "stop_loss"
    assert orders[0].trigger_price == pytest.approx(95000.0)
    assert orders[0].quantity == pytest.approx(0.2)


async def test_get_open_conditional_orders_ignores_closed_and_non_conditional_orders() -> None:
    adapter, exchange, _ = _build_adapter()
    exchange.fetch_positions = AsyncMock(return_value=[])

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://api\.bybit\.com/v5/order/realtime\?.*$"),
            status=200,
            payload={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {
                            "orderId": "entry-filled",
                            "orderLinkId": "entry-1",
                            "orderType": "Market",
                            "orderStatus": "Filled",
                            "qty": "0.002",
                        },
                        {
                            "orderId": "sl-cancelled",
                            "orderLinkId": "sl-1",
                            "stopOrderType": "StopLoss",
                            "orderStatus": "Cancelled",
                            "triggerPrice": "95000",
                            "qty": "0.002",
                        },
                        {
                            "orderId": "tp-open",
                            "orderLinkId": "tp-1",
                            "stopOrderType": "TakeProfit",
                            "orderStatus": "Untriggered",
                            "triggerPrice": "105000",
                            "qty": "0.001",
                        },
                    ]
                },
            },
        )
        orders = await adapter.get_open_conditional_orders("BTC/USDT:USDT")

    assert len(orders) == 1
    assert orders[0].exchange_order_id == "tp-open"
    assert orders[0].order_type == "take_profit"
    assert orders[0].status == "new"


async def test_cancel_conditional_order_cancels_full_position_stop_loss_via_trading_stop() -> None:
    adapter, _, _ = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            TRADING_STOP_URL,
            status=200,
            payload={"retCode": 0, "retMsg": "OK", "result": {}},
        )
        cancelled = await adapter.cancel_conditional_order(
            "BTC/USDT:USDT",
            "bybit-position-tpsl:stop_loss:BTCUSDT",
        )

    assert cancelled is True
    body = _extract_last_request_json(mocked, "POST", "/v5/position/trading-stop")
    assert body["symbol"] == "BTCUSDT"
    assert body["tpslMode"] == "Full"
    assert body["stopLoss"] == "0"
    assert body["positionIdx"] == 0


async def test_clear_symbol_conditional_orders_resets_tpsl_and_cancels_all_orders() -> None:
    adapter, _, _ = _build_adapter()

    with aioresponses() as mocked:
        mocked.post(
            TRADING_STOP_URL,
            status=200,
            payload={"retCode": 0, "retMsg": "OK", "result": {}},
        )
        mocked.post(
            re.compile(r"^https://api\.bybit\.com/v5/order/cancel-all$"),
            status=200,
            payload={"retCode": 0, "retMsg": "OK", "result": {"list": []}},
        )
        await adapter.clear_symbol_conditional_orders("BTC/USDT:USDT")

    trading_stop_body = _extract_last_request_json(mocked, "POST", "/v5/position/trading-stop")
    assert trading_stop_body["symbol"] == "BTCUSDT"
    assert trading_stop_body["takeProfit"] == "0"
    assert trading_stop_body["stopLoss"] == "0"
    assert trading_stop_body["trailingStop"] == "0"
    assert trading_stop_body["tpslMode"] == "Full"

    cancel_all_body = _extract_last_request_json(mocked, "POST", "/v5/order/cancel-all")
    assert cancel_all_body["category"] == "linear"
    assert cancel_all_body["symbol"] == "BTCUSDT"


async def test_get_position_maps_ccxt_response_to_position_snapshot() -> None:
    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC/USDT:USDT",
                "contracts": "1.5",
                "side": "sell",
                "entryPrice": "101100.0",
                "unrealizedPnl": "-12.5",
                "leverage": "15",
                "markPrice": "101250.0",
                "liquidationPrice": "112500.0",
            }
        ]
    )
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    position = await adapter.get_position("BTC/USDT:USDT")

    assert position is not None
    assert position.side == PositionSide.SHORT
    assert position.size == pytest.approx(1.5)
    assert position.entry_price == pytest.approx(101100.0)
    assert position.unrealized_pnl == pytest.approx(-12.5)
    assert position.leverage == 15
    assert position.mark_price == pytest.approx(101250.0)
    assert position.liquidation_price == pytest.approx(112500.0)
    assert position.open_orders == []


async def test_partial_close_uses_ccxt_create_order_with_reduce_only() -> None:
    exchange = MagicMock()
    exchange.create_order = AsyncMock(
        return_value={
            "id": "close-bybit-1",
            "filled": 0.3,
            "average": 102_000.0,
            "remaining": 0.2,
            "fee": {"cost": 0.02},
        }
    )
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    result = await adapter.partial_close(
        symbol="BTC/USDT:USDT",
        side=OrderSide.BUY,
        quantity=0.5,
        client_order_id="close-cid-1",
    )

    exchange.create_order.assert_awaited_once()
    _, kwargs = exchange.create_order.call_args
    assert kwargs["params"]["reduceOnly"] is True
    assert kwargs["params"]["orderLinkId"] == "close-cid-1"

    assert result.order_id == "close-bybit-1"
    assert result.executed_qty == pytest.approx(0.3)
    assert result.remaining_qty == pytest.approx(0.2)
    assert result.avg_price == pytest.approx(102000.0)
    assert result.commission == pytest.approx(0.02)


async def test_subscribe_user_data_authenticates_subscribes_and_routes_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, _ = _build_adapter()
    fake_ws = _FakeWebSocket(
        messages=[
            json.dumps({"op": "auth", "retCode": 0}),
            json.dumps({"op": "subscribe", "retCode": 0}),
            json.dumps(
                {
                    "topic": "order",
                    "creationTime": 1710000000100,
                    "data": [
                        {
                            "symbol": "BTCUSDT",
                            "orderId": "order-1",
                            "orderLinkId": "client-1",
                            "orderStatus": "New",
                            "orderType": "Limit",
                            "stopOrderType": "StopLoss",
                            "triggerPrice": "95000",
                            "price": "94950",
                            "qty": "0.01",
                            "cumExecQty": "0",
                            "leavesQty": "0.01",
                            "side": "Sell",
                            "reduceOnly": True,
                            "closeOnTrigger": True,
                            "updatedTime": "1710000000101",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "topic": "execution",
                    "creationTime": 1710000000200,
                    "data": [
                        {
                            "symbol": "BTCUSDT",
                            "orderId": "order-1",
                            "orderLinkId": "client-1",
                            "orderType": "Market",
                            "stopOrderType": "UNKNOWN",
                            "execPrice": "95900.1",
                            "execQty": "0.01",
                            "leavesQty": "0",
                            "orderQty": "0.01",
                            "execType": "Trade",
                            "side": "Sell",
                            "markPrice": "95901.48",
                            "execFee": "0.02",
                            "execTime": "1710000000201",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "topic": "position",
                    "creationTime": 1710000000300,
                    "data": [
                        {
                            "symbol": "BTCUSDT",
                            "side": "Sell",
                            "size": "0.5",
                            "avgPrice": "101100",
                            "markPrice": "101250",
                            "liqPrice": "112500",
                            "leverage": "10",
                            "unrealisedPnl": "-12.5",
                            "positionStatus": "Normal",
                            "takeProfit": "0",
                            "stopLoss": "95000",
                            "trailingStop": "0",
                            "updatedTime": "1710000000301",
                        }
                    ],
                }
            ),
        ]
    )
    connect_mock = MagicMock(return_value=fake_ws)
    monkeypatch.setattr("app.services.exchange.bybit_adapter.websockets.connect", connect_mock)
    monkeypatch.setattr("app.services.exchange.bybit_adapter.time.time", lambda: 100.0)

    on_order_update = AsyncMock()
    on_position_update = AsyncMock()
    on_disconnect = AsyncMock()

    await adapter.subscribe_user_data(on_order_update, on_position_update, on_disconnect)
    assert adapter._user_data_task is not None
    await asyncio.wait_for(adapter._user_data_task, timeout=1.0)

    connect_mock.assert_called_once_with(
        "wss://stream.bybit.com/v5/private",
        ping_interval=None,
        ping_timeout=None,
    )
    assert len(fake_ws.sent_messages) >= 2

    auth_payload = fake_ws.sent_messages[0]
    subscribe_payload = fake_ws.sent_messages[1]
    expected_signature = adapter._sign_ws_auth(110000)

    assert auth_payload == {
        "op": "auth",
        "args": ["api-key", 110000, expected_signature],
    }
    assert subscribe_payload == {
        "op": "subscribe",
        "args": ["order", "execution", "position"],
    }

    assert on_order_update.await_count == 2
    order_event = on_order_update.await_args_list[0].args[0]
    execution_event = on_order_update.await_args_list[1].args[0]
    position_event = on_position_update.await_args.args[0]

    assert order_event["type"] == "order"
    assert order_event["symbol"] == "BTCUSDT"
    assert order_event["order_id"] == "order-1"
    assert order_event["status"] == "new"
    assert order_event["order_type"] == "stop_loss"
    assert order_event["price"] == pytest.approx(95000.0)
    assert order_event["quantity"] == pytest.approx(0.01)
    assert order_event["side"] == "sell"

    assert execution_event["type"] == "execution"
    assert execution_event["status"] == "triggered"
    assert execution_event["price"] == pytest.approx(95900.1)
    assert execution_event["quantity"] == pytest.approx(0.01)
    assert execution_event["mark_price"] == pytest.approx(95901.48)

    assert position_event["type"] == "position"
    assert position_event["symbol"] == "BTCUSDT"
    assert position_event["size"] == pytest.approx(0.5)
    assert position_event["position_amount"] == pytest.approx(-0.5)
    assert position_event["stop_loss"] == pytest.approx(95000.0)

    on_disconnect.assert_awaited_once()


async def test_subscribe_user_data_calls_disconnect_on_stream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, _ = _build_adapter()
    fake_ws = _FakeWebSocket(
        messages=[json.dumps({"op": "auth", "retCode": 0})],
        iteration_error=RuntimeError("socket-failed"),
    )
    monkeypatch.setattr(
        "app.services.exchange.bybit_adapter.websockets.connect",
        MagicMock(return_value=fake_ws),
    )

    on_disconnect = AsyncMock()
    await adapter.subscribe_user_data(AsyncMock(), AsyncMock(), on_disconnect)
    assert adapter._user_data_task is not None
    await asyncio.wait_for(adapter._user_data_task, timeout=1.0)

    on_disconnect.assert_awaited_once()


async def test_private_ping_sends_ping_every_twenty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, _ = _build_adapter()
    fake_ws = _FakeWebSocket()
    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr("app.services.exchange.bybit_adapter.asyncio.sleep", sleep_mock)

    with pytest.raises(asyncio.CancelledError):
        await adapter._run_private_ping(fake_ws)

    sleep_mock.assert_any_await(20)
    assert fake_ws.sent_messages == [{"op": "ping"}]


async def test_subscribe_kline_subscribes_and_routes_normalized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, _ = _build_adapter()
    fake_ws = _FakeWebSocket(
        messages=[
            json.dumps({"op": "subscribe", "retCode": 0}),
            json.dumps(
                {
                    "topic": "kline.1.BTCUSDT",
                    "ts": 1672324988882,
                    "type": "snapshot",
                    "data": [
                        {
                            "start": 1672324800000,
                            "end": 1672324859999,
                            "interval": "1",
                            "open": "16649.5",
                            "close": "16677",
                            "high": "16677",
                            "low": "16608",
                            "volume": "2.081",
                            "turnover": "34666.4005",
                            "confirm": False,
                            "timestamp": 1672324988882,
                        }
                    ],
                }
            ),
        ]
    )
    connect_mock = MagicMock(return_value=fake_ws)
    monkeypatch.setattr("app.services.exchange.bybit_adapter.websockets.connect", connect_mock)

    on_kline = AsyncMock()
    await adapter.subscribe_kline("BTC/USDT:USDT", "1m", on_kline)

    task = adapter._kline_tasks["kline.1.BTCUSDT"]
    await asyncio.wait_for(task, timeout=1.0)

    connect_mock.assert_called_once_with(
        "wss://stream.bybit.com/v5/public/linear",
        ping_interval=None,
        ping_timeout=None,
    )
    assert fake_ws.sent_messages[0] == {
        "op": "subscribe",
        "args": ["kline.1.BTCUSDT"],
    }

    on_kline.assert_awaited_once()
    event = on_kline.await_args.args[0]
    assert event["symbol"] == "BTCUSDT"
    assert event["interval"] == "1m"
    assert event["open"] == pytest.approx(16649.5)
    assert event["close"] == pytest.approx(16677.0)
    assert event["quote_volume"] == pytest.approx(34666.4005)
    assert event["is_closed"] is False


def test_kline_interval_mapping_round_trips_common_values() -> None:
    adapter, _, _ = _build_adapter()

    assert adapter._to_bybit_kline_interval("1m") == "1"
    assert adapter._to_bybit_kline_interval("1h") == "60"
    assert adapter._from_bybit_kline_interval("240") == "4h"
    assert adapter._from_bybit_kline_interval("D") == "1d"


# ─── place_entry_order: bracket-at-entry ──────────────────────────────────────


def _entry_ccxt_payload(*, order_id: str = "entry-1", filled: float = 0.5) -> dict[str, Any]:
    return {
        "id": order_id,
        "clientOrderId": "client-entry-1",
        "orderLinkId": "client-entry-1",
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


async def test_place_entry_order_no_protective_omits_tpsl_params() -> None:
    exchange = MagicMock()
    exchange.create_order = AsyncMock(return_value=_entry_ccxt_payload())
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    result = await adapter.place_entry_order(
        symbol="BTC/USDT:USDT",
        side=OrderSide.BUY,
        quantity=0.5,
        client_order_id="client-entry-1",
    )

    exchange.create_order.assert_awaited_once()
    _, kwargs = exchange.create_order.call_args
    assert "takeProfit" not in kwargs["params"]
    assert "stopLoss" not in kwargs["params"]
    assert kwargs["params"]["reduceOnly"] is False
    assert result.attached_sl is None
    assert result.attached_tp is None


async def test_place_entry_order_with_bracket_passes_native_params() -> None:
    exchange = MagicMock()
    exchange.create_order = AsyncMock(return_value=_entry_ccxt_payload())
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    result = await adapter.place_entry_order(
        symbol="BTC/USDT:USDT",
        side=OrderSide.BUY,
        quantity=0.5,
        client_order_id="client-entry-1",
        stop_loss_price=95_000.0,
        take_profit_price=105_000.0,
        sl_client_order_id="client-entry-1-sl",
        tp_client_order_id="client-entry-1-tp",
    )

    exchange.create_order.assert_awaited_once()
    _, kwargs = exchange.create_order.call_args
    params = kwargs["params"]
    assert params["takeProfit"] == "105000.0"
    assert params["stopLoss"] == "95000.0"
    assert params["tpTriggerBy"] == "MarkPrice"
    assert params["slTriggerBy"] == "MarkPrice"
    assert params["tpOrderType"] == "Market"
    assert params["slOrderType"] == "Market"
    assert params["reduceOnly"] is False

    assert result.attached_sl is not None
    assert result.attached_sl.order_type == "stop_loss"
    assert result.attached_sl.trigger_price == pytest.approx(95_000.0)
    assert result.attached_sl.client_order_id == "client-entry-1-sl"
    assert result.attached_tp is not None
    assert result.attached_tp.order_type == "take_profit"
    assert result.attached_tp.trigger_price == pytest.approx(105_000.0)
    assert result.attached_tp.client_order_id == "client-entry-1-tp"


async def test_place_entry_order_only_sl_passes_only_sl_params() -> None:
    exchange = MagicMock()
    exchange.create_order = AsyncMock(return_value=_entry_ccxt_payload())
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    result = await adapter.place_entry_order(
        symbol="BTC/USDT:USDT",
        side=OrderSide.SELL,
        quantity=0.5,
        client_order_id="client-entry-2",
        stop_loss_price=105_000.0,
    )

    _, kwargs = exchange.create_order.call_args
    params = kwargs["params"]
    assert params["stopLoss"] == "105000.0"
    assert "takeProfit" not in params
    assert "tpTriggerBy" not in params
    assert result.attached_sl is not None
    assert result.attached_tp is None


# ─── precision: -1111 regression ──────────────────────────────────────────────


async def test_place_entry_order_rounds_quantity_and_bracket_to_market_precision() -> None:
    """Regression: Bybit V5 also rejects raw floats with too many decimals."""
    exchange = MagicMock()
    exchange.create_order = AsyncMock(return_value=_entry_ccxt_payload())
    adapter, _, _ = _build_adapter(ccxt_exchange=exchange)

    await adapter.place_entry_order(
        symbol="BTC/USDT:USDT",
        side=OrderSide.BUY,
        quantity=0.0015289123,
        client_order_id="cid-rnd",
        stop_loss_price=64777.7790,
        take_profit_price=66000.4567,
    )

    args, kwargs = exchange.create_order.call_args
    assert args[3] == pytest.approx(0.001)
    params = kwargs["params"]
    assert params["stopLoss"] == "64777.8"
    assert params["takeProfit"] == "66000.5"


async def test_place_stop_loss_rounds_trigger_price_to_market_precision() -> None:
    """Bybit /v5/position/trading-stop must receive tick-aligned stopLoss."""
    adapter, _, _ = _build_adapter()
    with aioresponses() as mocked:
        mocked.post(
            TRADING_STOP_URL,
            status=200,
            payload={"retCode": 0, "retMsg": "OK", "result": {}},
        )
        await adapter.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=0.0015289123,
            trigger_price=64777.7790,
            client_order_id="sl-rnd",
        )

    body = _extract_last_request_json(mocked, "POST", "/v5/position/trading-stop")
    assert body["stopLoss"] == "64777.8"
