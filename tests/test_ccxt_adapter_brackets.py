from typing import Any

import ccxt.async_support as ccxt

from app.schemas.exchange_trading import AttachedTriggerOrder
from app.services.execution.base import ExchangeCredentials
from app.services.execution.ccxt_adapter import CcxtAdapter


class _FakeBybitExchange:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.create_calls: list[dict[str, Any]] = []
        self.closed = False

    async def load_markets(self) -> dict[str, object]:
        return {}

    async def close(self) -> None:
        self.closed = True

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
        if len(self.create_calls) == 1:
            return {
                "id": "entry-ord",
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "status": "closed",
                "amount": amount,
                "filled": 0.4,
                "remaining": 0.6,
                "info": {"orderLinkId": "entry-client"},
            }
        return {
            "id": f"child-{len(self.create_calls)}",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "status": "open",
            "amount": amount,
            "filled": 0.0,
            "remaining": amount,
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

    async def fetch_my_trades(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        return []


async def test_bybit_market_entry_creates_tp_sl_with_filled_size(
    monkeypatch,
) -> None:
    fake_exchange = _FakeBybitExchange({})

    def _factory(_: dict[str, object]) -> _FakeBybitExchange:
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

    result = await adapter.place_spot_order(
        symbol="BTC/USDT",
        side="buy",
        order_type="market",
        amount=1.0,
        client_order_id="entry-client",
        attached_take_profit=AttachedTriggerOrder(trigger_price=69000, order_type="market"),
        attached_stop_loss=AttachedTriggerOrder(trigger_price=65000, order_type="market"),
    )

    assert result.id == "entry-ord"
    assert len(fake_exchange.create_calls) == 3

    tp_call = fake_exchange.create_calls[1]
    sl_call = fake_exchange.create_calls[2]

    assert tp_call["side"] == "sell"
    assert sl_call["side"] == "sell"
    assert tp_call["amount"] == 0.4
    assert sl_call["amount"] == 0.4
    assert tp_call["params"]["takeProfitPrice"] == 69000
    assert sl_call["params"]["stopLossPrice"] == 65000
    assert tp_call["params"]["orderLinkId"] == "entry-client-tp"
    assert sl_call["params"]["orderLinkId"] == "entry-client-sl"
