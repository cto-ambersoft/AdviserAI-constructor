import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.main import app
from app.models.user import User


@pytest.fixture(autouse=True)
def override_current_user() -> None:
    async def _fake_current_user() -> User:
        return User(id=1, email="test@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def test_market_meta_endpoint_returns_query_constraints() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/market/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["default_symbol"] == "BTC/USDT"
    assert body["default_timeframe"] == "1h"
    assert body["default_bars"] == 500
    assert "15m" in body["common_timeframes"]


async def test_strategy_meta_endpoint_returns_supported_types() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/strategies/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["default_strategy_type"] == "builder_vwap"
    assert body["default_version"] == "1.0.0"
    assert "intraday_momentum" in body["supported_strategy_types"]


async def test_audit_meta_endpoint_returns_defaults_and_suggestions() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/audit/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["default_target_type"] == "system"
    assert body["default_target_id"] == "n/a"
    assert body["list_limit_default"] == 200
    assert "BUILDER_CHANGE" in body["suggested_events"]
