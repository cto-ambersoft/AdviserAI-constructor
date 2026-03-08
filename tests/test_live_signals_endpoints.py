from datetime import UTC, datetime, timedelta

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


def _candles(count: int = 180) -> list[dict[str, float | str]]:
    base = datetime(2025, 1, 1, tzinfo=UTC)
    rows: list[dict[str, float | str]] = []
    price = 100.0
    for i in range(count):
        open_price = price
        close_price = price + 0.3
        high = close_price + 0.5
        low = open_price - 0.5
        rows.append(
            {
                "time": (base + timedelta(hours=i)).isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": 1000.0 + i,
            }
        )
        price = close_price
    return rows


async def test_builder_live_signal_endpoint_rejects_legacy_paper_mode() -> None:
    payload = {
        "signal": {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "bars": 180,
            "candles": _candles(),
            "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
            "regime": "Bull",
            "stop_mode": "ATR",
        },
        "execution": {"mode": "paper", "execute": True, "fee_pct": 0.06},
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/live/signals/builder", json=payload)
    assert response.status_code == 422


async def test_builder_live_signal_endpoint_supports_dry_run() -> None:
    payload = {
        "signal": {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "bars": 180,
            "candles": _candles(),
            "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
            "regime": "Bull",
            "stop_mode": "ATR",
        },
        "execution": {"mode": "dry_run", "execute": False},
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/live/signals/builder", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert "execution" in body


async def test_atr_ob_live_signal_endpoint_returns_contract_shape() -> None:
    payload = {
        "signal": {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "bars": 180,
            "candles": _candles(),
            "ema_period": 50,
            "atr_period": 14,
        },
        "execution": {"mode": "dry_run", "execute": False},
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/live/signals/atr-order-block", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "has_signal",
        "side",
        "entry",
        "sl",
        "tp",
        "bar_time",
        "reasons",
        "sizing",
        "sl_explain",
        "execution",
    }
