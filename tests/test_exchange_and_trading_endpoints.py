from datetime import UTC, datetime

import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.v1.endpoints import market, trading
from app.main import app
from app.models.user import User
from app.schemas.exchange_trading import (
    NormalizedOrder,
    SpotOrderRead,
    SpotOrdersRead,
)


@pytest.fixture(autouse=True)
def override_current_user() -> None:
    async def _fake_current_user() -> User:
        return User(id=1, email="test@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def test_exchange_meta_has_supported_modes_and_exchanges() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/exchange/accounts/meta")
    assert response.status_code == 200
    body = response.json()
    assert "bybit" in body["supported_exchanges"]
    assert "real" in body["supported_modes"]
    assert body["default_mode"] == "demo"


async def test_market_ohlcv_accepts_exchange_name(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch_ohlcv(
        *, exchange_name: str, symbol: str, timeframe: str, bars: int, cache_ttl_seconds: int = 60
    ) -> pd.DataFrame:
        assert exchange_name == "bybit"
        assert symbol == "BTC/USDT"
        assert timeframe == "1h"
        assert bars == 100
        return (
            pd.DataFrame(
                [
                    {
                        "time": "2026-01-01T00:00:00Z",
                        "open": 1,
                        "high": 2,
                        "low": 0.5,
                        "close": 1.5,
                        "volume": 10,
                    },
                    {
                        "time": "2026-01-01T01:00:00Z",
                        "open": 1.5,
                        "high": 2.2,
                        "low": 1.4,
                        "close": 2.0,
                        "volume": 12,
                    },
                ]
            )
            .assign(time=lambda df: pd.to_datetime(df["time"], utc=True))
            .set_index("time")
        )

    monkeypatch.setattr(market.market_data, "fetch_ohlcv", _fake_fetch_ohlcv)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/market/ohlcv",
            params={"exchange_name": "bybit", "symbol": "BTC/USDT", "timeframe": "1h", "bars": 100},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["exchange_name"] == "bybit"
    assert len(body["rows"]) == 2


async def test_trading_place_order_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_place_spot_order(*, session, user_id: int, payload) -> SpotOrderRead:
        assert user_id == 1
        assert payload.attached_take_profit is not None
        assert payload.attached_take_profit.trigger_price == 51000
        assert payload.attached_stop_loss is not None
        assert payload.attached_stop_loss.trigger_price == 47000
        return SpotOrderRead(
            account_id=payload.account_id,
            exchange_name="bybit",
            mode="real",
            order=NormalizedOrder(
                id="ord-1",
                symbol=payload.symbol,
                side=payload.side,
                order_type=payload.order_type,
                status="open",
                amount=payload.amount,
                filled=0.0,
                remaining=payload.amount,
                timestamp=datetime.now(UTC),
            ),
        )

    monkeypatch.setattr(trading.trading_service, "place_spot_order", _fake_place_spot_order)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/trading/spot/orders",
            json={
                "account_id": 2,
                "symbol": "BTC/USDT",
                "side": "buy",
                "order_type": "market",
                "amount": 0.01,
                "attached_take_profit": {"trigger_price": 51000, "order_type": "market"},
                "attached_stop_loss": {"trigger_price": 47000, "order_type": "market"},
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["exchange_name"] == "bybit"
    assert body["order"]["id"] == "ord-1"


async def test_trading_place_limit_order_requires_price() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/trading/spot/orders",
            json={
                "account_id": 2,
                "symbol": "BTC/USDT",
                "side": "buy",
                "order_type": "limit",
                "amount": 0.01,
            },
        )
    assert response.status_code == 422


async def test_attached_limit_tp_requires_price() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/trading/spot/orders",
            json={
                "account_id": 2,
                "symbol": "BTC/USDT",
                "side": "buy",
                "order_type": "market",
                "amount": 0.01,
                "attached_take_profit": {"trigger_price": 52000, "order_type": "limit"},
            },
        )
    assert response.status_code == 422


async def test_trading_open_orders_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_spot_open_orders(
        *, session, user_id: int, account_id: int, symbol: str | None, limit: int
    ) -> SpotOrdersRead:
        assert user_id == 1
        return SpotOrdersRead(
            account_id=account_id,
            exchange_name="bybit",
            mode="demo",
            orders=[
                NormalizedOrder(
                    id="ord-2",
                    symbol=symbol or "BTC/USDT",
                    side="buy",
                    order_type="limit",
                    status="open",
                    amount=1.0,
                    filled=0.0,
                    remaining=1.0,
                )
            ],
        )

    monkeypatch.setattr(trading.trading_service, "get_spot_open_orders", _fake_get_spot_open_orders)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/trading/spot/orders/open",
            params={"account_id": 2, "symbol": "BTC/USDT", "limit": 10},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "demo"
    assert body["orders"][0]["id"] == "ord-2"


async def test_trading_order_detail_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_spot_order_detail(
        *,
        session,
        user_id: int,
        account_id: int,
        order_id: str,
        symbol: str,
    ) -> SpotOrderRead:
        assert user_id == 1
        assert order_id == "ord-42"
        assert symbol == "BTC/USDT"
        return SpotOrderRead(
            account_id=account_id,
            exchange_name="bybit",
            mode="demo",
            order=NormalizedOrder(
                id=order_id,
                symbol=symbol,
                side="buy",
                order_type="market",
                status="closed",
                amount=0.02,
                filled=0.02,
                remaining=0.0,
            ),
        )

    monkeypatch.setattr(
        trading.trading_service, "get_spot_order_detail", _fake_get_spot_order_detail
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/trading/spot/orders/detail/ord-42",
            params={"account_id": 2, "symbol": "BTC/USDT"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["order"]["id"] == "ord-42"
