from datetime import UTC, datetime
from typing import Any, cast

import ccxt.async_support as ccxt

from app.services.execution.base import ExchangeCredentials
from app.services.execution.ccxt_adapter import CcxtAdapter


class _FakeBybitFuturesExchange:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.closed = False
        self.set_leverage_calls: list[dict[str, object]] = []
        self.set_margin_mode_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []
        self.fetch_my_trades_calls: list[dict[str, object]] = []
        self.raise_set_leverage_error: Exception | None = None
        self.raise_set_margin_mode_error: Exception | None = None

    async def load_markets(self) -> dict[str, object]:
        return {}

    async def close(self) -> None:
        self.closed = True

    async def set_leverage(
        self,
        leverage: int,
        symbol: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.set_leverage_calls.append(
            {"leverage": leverage, "symbol": symbol, "params": params or {}}
        )
        if self.raise_set_leverage_error is not None:
            raise self.raise_set_leverage_error
        return {"ok": True}

    async def set_margin_mode(
        self,
        margin_mode: str,
        symbol: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.set_margin_mode_calls.append(
            {
                "margin_mode": margin_mode,
                "symbol": symbol,
                "params": params or {},
            }
        )
        if self.raise_set_margin_mode_error is not None:
            raise self.raise_set_margin_mode_error
        return {"ok": True}

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.create_calls.append(
            {
                "symbol": symbol,
                "order_type": order_type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": params or {},
            }
        )
        return {
            "id": "fut-1",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "status": "closed",
            "amount": amount,
            "filled": amount,
            "remaining": 0.0,
            "average": 100.0,
            "price": 100.0,
            "info": {"orderLinkId": (params or {}).get("orderLinkId", "")},
        }

    async def fetch_open_orders(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        return []

    async def fetch_closed_orders(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        return []

    async def fetch_positions(
        self,
        symbols: list[str] | None = None,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        symbol = symbols[0] if symbols else "BTC/USDT:USDT"
        return [
            {
                "symbol": symbol,
                "contracts": 0.4,
                "side": "long",
                "entryPrice": 100.0,
                "markPrice": 101.0,
                "leverage": 3,
                "unrealizedPnl": 0.4,
                "marginMode": "isolated",
                "info": {
                    "side": "Buy",
                    "size": "0.4",
                    "takeProfit": "110.0",
                    "stopLoss": "95.0",
                    "liqPrice": "80.0",
                    "positionValue": "40.4",
                    "positionIM": "13.5",
                },
            }
        ]

    async def fetch_my_trades(
        self,
        symbol: str,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        self.fetch_my_trades_calls.append(
            {
                "symbol": symbol,
                "since": since,
                "limit": limit,
                "params": params or {},
            }
        )
        return [
            {
                "id": "1001",
                "order": "ord-1",
                "symbol": symbol,
                "side": "buy",
                "amount": 0.4,
                "price": 100.0,
                "cost": 40.0,
                "timestamp": 1700000000000,
                "info": {"clientOrderId": "cid-1"},
                "fee": {"cost": 0.01, "currency": "USDT"},
            }
        ]


class _FakeBinanceUsdmExchange(_FakeBybitFuturesExchange):
    async def fetch_positions(
        self,
        symbols: list[str] | None = None,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        symbol = symbols[0] if symbols else "BTC/USDT:USDT"
        return [
            {
                "symbol": symbol,
                "contracts": 0.6,
                "side": "long",
                "entryPrice": 101.0,
                "markPrice": 102.5,
                "leverage": 5,
                "unrealizedPnl": 0.9,
                "info": {
                    "positionAmt": "0.6",
                    "liquidationPrice": "70.0",
                    "notional": "61.5",
                    "isolatedWallet": "12.0",
                    "isolated": True,
                },
            }
        ]


async def test_set_leverage_and_place_futures_market_order(monkeypatch) -> None:
    fake_exchange = _FakeBybitFuturesExchange({})

    def _factory(_: dict[str, object]) -> _FakeBybitFuturesExchange:
        return fake_exchange

    monkeypatch.setattr(ccxt, "bybit", _factory)
    adapter = CcxtAdapter(
        ExchangeCredentials(
            exchange_name="bybit",
            api_key="k",
            api_secret="s",
            mode="real",
        )
    )

    await adapter.set_futures_leverage(symbol="BTC/USDT:USDT", leverage=3)
    order = await adapter.place_futures_market_order(
        symbol="BTC/USDT:USDT",
        side="buy",
        amount=0.4,
        reduce_only=True,
        client_order_id="fut-client-1",
        take_profit_price=110.0,
        stop_loss_price=95.0,
    )

    assert len(fake_exchange.set_margin_mode_calls) == 2
    assert fake_exchange.set_margin_mode_calls[0]["margin_mode"] == "isolated"
    assert len(fake_exchange.set_leverage_calls) == 1
    assert fake_exchange.set_leverage_calls[0]["params"] == {"category": "linear"}
    assert len(fake_exchange.create_calls) == 1
    params = cast(dict[str, Any], fake_exchange.create_calls[0]["params"])
    assert params["reduceOnly"] is True
    assert params["takeProfit"] == 110.0
    assert params["stopLoss"] == 95.0
    assert params["tpslMode"] == "Full"
    assert params["tpTriggerBy"] == "MarkPrice"
    assert params["slTriggerBy"] == "MarkPrice"
    assert params["orderLinkId"] == "fut-client-1"
    assert order.id == "fut-1"


async def test_fetch_futures_position_returns_normalized_payload(monkeypatch) -> None:
    fake_exchange = _FakeBybitFuturesExchange({})

    def _factory(_: dict[str, object]) -> _FakeBybitFuturesExchange:
        return fake_exchange

    monkeypatch.setattr(ccxt, "bybit", _factory)
    adapter = CcxtAdapter(
        ExchangeCredentials(
            exchange_name="bybit",
            api_key="k",
            api_secret="s",
            mode="real",
        )
    )
    position = await adapter.fetch_futures_position(symbol="BTC/USDT:USDT")
    assert position is not None
    assert position.symbol == "BTC/USDT:USDT"
    assert position.side == "long"
    assert position.contracts == 0.4
    assert position.margin_mode == "isolated"
    assert position.take_profit_price == 110.0
    assert position.stop_loss_price == 95.0
    assert position.liquidation_price == 80.0


async def test_set_leverage_ignores_bybit_not_modified_error(monkeypatch) -> None:
    fake_exchange = _FakeBybitFuturesExchange({})
    fake_exchange.raise_set_leverage_error = ccxt.ExchangeError(
        'bybit {"retCode":110043,"retMsg":"leverage not modified",'
        '"result":{},"retExtInfo":{},"time":1773061237650}'
    )

    def _factory(_: dict[str, object]) -> _FakeBybitFuturesExchange:
        return fake_exchange

    monkeypatch.setattr(ccxt, "bybit", _factory)
    adapter = CcxtAdapter(
        ExchangeCredentials(
            exchange_name="bybit",
            api_key="k",
            api_secret="s",
            mode="real",
        )
    )

    await adapter.set_futures_leverage(symbol="BTC/USDT:USDT", leverage=1)
    assert len(fake_exchange.set_leverage_calls) == 1


async def test_set_leverage_ignores_bybit_not_modified_by_ret_code(monkeypatch) -> None:
    fake_exchange = _FakeBybitFuturesExchange({})
    fake_exchange.raise_set_leverage_error = ccxt.BadRequest(
        'bybit {"retCode":110043,"retMsg":"request completed with no state change",'
        '"result":{},"retExtInfo":{},"time":1773061237650}'
    )

    def _factory(_: dict[str, object]) -> _FakeBybitFuturesExchange:
        return fake_exchange

    monkeypatch.setattr(ccxt, "bybit", _factory)
    adapter = CcxtAdapter(
        ExchangeCredentials(
            exchange_name="bybit",
            api_key="k",
            api_secret="s",
            mode="real",
        )
    )

    await adapter.set_futures_leverage(symbol="BTC/USDT:USDT", leverage=7)
    assert len(fake_exchange.set_leverage_calls) == 1


async def test_binanceusdm_futures_are_supported(monkeypatch) -> None:
    fake_exchange = _FakeBinanceUsdmExchange({})

    def _factory(_: dict[str, object]) -> _FakeBinanceUsdmExchange:
        return fake_exchange

    monkeypatch.setattr(ccxt, "binanceusdm", _factory)
    adapter = CcxtAdapter(
        ExchangeCredentials(
            exchange_name="binance",
            api_key="k",
            api_secret="s",
            mode="real",
        )
    )

    await adapter.set_futures_leverage(symbol="BTC/USDT:USDT", leverage=5)
    order = await adapter.place_futures_market_order(
        symbol="BTC/USDT:USDT",
        side="buy",
        amount=0.6,
        reduce_only=True,
        client_order_id="bin-client-1",
    )
    position = await adapter.fetch_futures_position(symbol="BTC/USDT:USDT")

    assert len(fake_exchange.set_margin_mode_calls) == 1
    assert fake_exchange.set_margin_mode_calls[0]["margin_mode"] == "isolated"
    assert len(fake_exchange.set_leverage_calls) == 1
    assert fake_exchange.set_leverage_calls[0]["params"] == {}
    assert len(fake_exchange.create_calls) == 1
    params = cast(dict[str, Any], fake_exchange.create_calls[0]["params"])
    assert params["reduceOnly"] is True
    assert params["newClientOrderId"] == "bin-client-1"
    assert order.id == "fut-1"
    assert position is not None
    assert position.contracts == 0.6
    assert position.margin_mode == "isolated"
    assert position.liquidation_price == 70.0


async def test_binance_futures_market_with_tp_sl_creates_conditional_orders(monkeypatch) -> None:
    fake_exchange = _FakeBinanceUsdmExchange({})

    def _factory(_: dict[str, object]) -> _FakeBinanceUsdmExchange:
        return fake_exchange

    monkeypatch.setattr(ccxt, "binanceusdm", _factory)
    adapter = CcxtAdapter(
        ExchangeCredentials(
            exchange_name="binance",
            api_key="k",
            api_secret="s",
            mode="real",
        )
    )

    order = await adapter.place_futures_market_order(
        symbol="BTC/USDT:USDT",
        side="buy",
        amount=0.6,
        reduce_only=False,
        client_order_id="bin-open-1",
        take_profit_price=110.0,
        stop_loss_price=95.0,
    )

    assert order.id == "fut-1"
    assert len(fake_exchange.create_calls) == 3

    entry_call = fake_exchange.create_calls[0]
    stop_call = fake_exchange.create_calls[1]
    take_call = fake_exchange.create_calls[2]

    assert entry_call["order_type"] == "market"
    assert entry_call["side"] == "buy"
    entry_params = cast(dict[str, Any], entry_call["params"])
    assert entry_params["reduceOnly"] is False
    assert entry_params["newClientOrderId"] == "bin-open-1"
    assert "takeProfit" not in entry_params
    assert "stopLoss" not in entry_params

    assert stop_call["order_type"] == "STOP_MARKET"
    assert stop_call["side"] == "sell"
    stop_params = cast(dict[str, Any], stop_call["params"])
    assert stop_params["reduceOnly"] is True
    assert stop_params["stopPrice"] == 95.0
    assert stop_params["workingType"] == "MARK_PRICE"
    assert stop_params["newClientOrderId"] == "bin-open-1-sl"

    assert take_call["order_type"] == "TAKE_PROFIT_MARKET"
    assert take_call["side"] == "sell"
    take_params = cast(dict[str, Any], take_call["params"])
    assert take_params["reduceOnly"] is True
    assert take_params["stopPrice"] == 110.0
    assert take_params["workingType"] == "MARK_PRICE"
    assert take_params["newClientOrderId"] == "bin-open-1-tp"


async def test_set_leverage_ignores_binance_margin_mode_not_modified(monkeypatch) -> None:
    fake_exchange = _FakeBinanceUsdmExchange({})
    fake_exchange.raise_set_margin_mode_error = ccxt.ExchangeError(
        'binanceusdm {"code":-4046,"msg":"No need to change margin type."}'
    )

    def _factory(_: dict[str, object]) -> _FakeBinanceUsdmExchange:
        return fake_exchange

    monkeypatch.setattr(ccxt, "binanceusdm", _factory)
    adapter = CcxtAdapter(
        ExchangeCredentials(
            exchange_name="binance",
            api_key="k",
            api_secret="s",
            mode="real",
        )
    )

    await adapter.set_futures_leverage(symbol="BTC/USDT:USDT", leverage=5)
    assert len(fake_exchange.set_margin_mode_calls) == 1
    assert len(fake_exchange.set_leverage_calls) == 1


async def test_binance_futures_trades_page_uses_from_id_cursor(monkeypatch) -> None:
    fake_exchange = _FakeBinanceUsdmExchange({})

    def _factory(_: dict[str, object]) -> _FakeBinanceUsdmExchange:
        return fake_exchange

    monkeypatch.setattr(ccxt, "binanceusdm", _factory)
    adapter = CcxtAdapter(
        ExchangeCredentials(
            exchange_name="binance",
            api_key="k",
            api_secret="s",
            mode="real",
        )
    )

    trades, cursor = await adapter.fetch_futures_trades_page(
        symbol="BTC/USDT:USDT",
        since=datetime(2026, 3, 17, tzinfo=UTC),
        limit=100,
        cursor="1000",
    )
    assert len(trades) == 1
    assert cursor == "1001"
    assert len(fake_exchange.fetch_my_trades_calls) == 1
    assert fake_exchange.fetch_my_trades_calls[0]["since"] is None
    params = cast(dict[str, Any], fake_exchange.fetch_my_trades_calls[0]["params"])
    assert params["fromId"] == "1000"
