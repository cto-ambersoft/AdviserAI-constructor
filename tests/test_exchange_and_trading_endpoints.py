import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.v1.endpoints import market
from app.main import app
from app.models.user import User


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


