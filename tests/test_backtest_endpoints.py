from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1.endpoints import backtest as backtest_endpoint
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base
from app.models.strategy import Strategy
from app.models.user import User
from app.services.backtesting.service import BacktestingService


@pytest.fixture(autouse=True)
def override_current_user() -> None:
    async def _fake_current_user() -> User:
        return User(id=1, email="test@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def backtest_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    db_path = tmp_path / "backtest_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db_session(backtest_db: async_sessionmaker[AsyncSession]) -> None:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with backtest_db() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session
    yield
    app.dependency_overrides.pop(get_db_session, None)


def _candles(count: int = 140) -> list[dict[str, float | str]]:
    base = datetime(2025, 1, 1, tzinfo=UTC)
    rows: list[dict[str, float | str]] = []
    price = 100.0
    for i in range(count):
        open_price = price
        close_price = price + 0.2
        high = close_price + 0.4
        low = open_price - 0.4
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


async def test_vwap_backtest_endpoint_returns_contract_shape() -> None:
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 140,
        "candles": _candles(),
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Bull",
        "include_series": False,
        "trades_limit": 200,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/vwap", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"summary", "trades", "chart_points", "explanations"}
    assert body["chart_points"] == {}


async def test_vwap_indicators_endpoint_returns_actual_allowlist() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/backtest/vwap/indicators")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"indicators"}
    assert isinstance(body["indicators"], list)
    assert body["indicators"] == sorted(body["indicators"])
    assert "VWAP" in body["indicators"]
    assert "EMA Fast (21)" in body["indicators"]


async def test_vwap_presets_and_regimes_endpoints() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        presets_response = await client.get("/api/v1/backtest/vwap/presets")
        regimes_response = await client.get("/api/v1/backtest/vwap/regimes")
    assert presets_response.status_code == 200
    assert presets_response.json()["presets"] == [
        "Custom",
        "Trend",
        "Range",
        "Breakdown",
        "Advanced Ichimoku",
        "Pivots+CCI",
    ]
    assert regimes_response.status_code == 200
    assert regimes_response.json()["regimes"] == ["Bull", "Flat", "Bear"]


async def test_backtest_catalog_endpoint_returns_client_form_metadata() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/backtest/catalog")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "vwap",
        "atr_order_block",
        "knife_catcher",
        "grid_bot",
        "intraday_momentum",
        "portfolio",
    }
    assert body["vwap"]["timeframes"] == ["15m", "1h", "4h"]
    assert body["vwap"]["stop_modes"] == ["ATR", "Swing", "Order Block (ATR-OB)"]
    assert body["knife_catcher"]["entry_mode_long"] == ["OPEN_LOW", "HIGH_LOW"]
    assert body["portfolio"]["builtin_strategies"] == [
        "VWAP Builder",
        "ATR Order-Block",
        "Knife Catcher",
        "Grid BOT",
        "Intraday Momentum",
    ]
    assert "builtin_strategy_params" in body["portfolio"]
    assert "VWAP Builder" in body["portfolio"]["builtin_strategy_params"]
    assert "enabled" in body["portfolio"]["builtin_strategy_params"]["VWAP Builder"]


async def test_vwap_backtest_uses_backend_market_fetch_when_candles_absent(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_load_market_frame(
        exchange_name: str,
        symbol: str,
        timeframe: str,
        bars: int,
        candles: list[dict[str, object]] | None = None,
    ) -> pd.DataFrame:
        captured["exchange_name"] = exchange_name
        captured["symbol"] = symbol
        captured["timeframe"] = timeframe
        captured["bars"] = bars
        captured["candles"] = candles

        base = datetime(2025, 1, 1, tzinfo=UTC)
        rows: list[dict[str, object]] = []
        price = 100.0
        for i in range(bars):
            open_price = price
            close_price = price + 0.2
            high = close_price + 0.4
            low = open_price - 0.4
            rows.append(
                {
                    "time": base + timedelta(hours=i),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close_price,
                    "volume": 1000.0 + i,
                }
            )
            price = close_price
        return pd.DataFrame(rows).set_index("time")

    monkeypatch.setattr(backtest_endpoint.service, "load_market_frame", _fake_load_market_frame)
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 140,
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Bull",
        "include_series": False,
        "trades_limit": 200,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/vwap", json=payload)
    assert response.status_code == 200
    assert captured == {
        "exchange_name": "bybit",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 140,
        "candles": None,
    }


async def test_portfolio_backtest_endpoint_returns_equity() -> None:
    payload = {
        "total_capital": 5000,
        "strategies": [
            {
                "name": "s1",
                "trades": [
                    {"exit_time": "2025-01-01T00:00:00+00:00", "pnl_usdt": 100},
                    {"exit_time": "2025-01-02T00:00:00+00:00", "pnl_usdt": -40},
                ],
            }
        ],
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/portfolio", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert "equity" in body["chart_points"]
    assert body["summary"]["final_equity"] == 5060.0
    assert "client_values" in body["summary"]
    assert body["summary"]["client_values"]["finalEquity"] == 5060.0


async def test_portfolio_backtest_supports_user_and_builtin_split_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_resolve_portfolio_strategies(payload, session, user_id: int):
        assert user_id == 1
        assert payload["user_strategies"][0]["strategy_id"] == 7
        assert payload["builtin_strategies"][0]["name"] == "Grid BOT"
        return [
            {
                "name": "Saved VWAP",
                "weight": 70.0,
                "config": {"strategy_type": "manual"},
                "trades": [{"exit_time": "2025-01-01T00:00:00+00:00", "pnl_usdt": 100.0}],
            },
            {
                "name": "Grid BOT",
                "weight": 30.0,
                "config": {"strategy_type": "manual"},
                "trades": [{"exit_time": "2025-01-02T00:00:00+00:00", "pnl_usdt": -50.0}],
            },
        ]

    monkeypatch.setattr(
        backtest_endpoint.service,
        "_resolve_portfolio_strategies",
        _fake_resolve_portfolio_strategies,
    )
    payload = {
        "total_capital": 10_000,
        "user_strategies": [{"strategy_id": 7, "allocation_pct": 70.0}],
        "builtin_strategies": [{"name": "Grid BOT", "allocation_pct": 30.0, "config": {}}],
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/portfolio", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["final_equity"] == 10050.0
    assert body["summary"]["allocated_capital"] == 10_000.0
    assert len(body["trades"]) == 2
    assert body["explanations"][0]["strategy"] == "Saved VWAP"


async def test_portfolio_service_resolves_saved_strategies_by_user_and_id(
    backtest_db: async_sessionmaker[AsyncSession],
) -> None:
    async with backtest_db() as session:
        row = Strategy(
            user_id=1,
            name="My Strategy",
            strategy_type="builder_vwap",
            config={"symbol": "BTC/USDT", "timeframe": "1h", "bars": 140, "enabled": ["VWAP"]},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        service = BacktestingService()
        resolved = await service._resolve_portfolio_strategies(
            payload={
                "user_strategies": [{"strategy_id": row.id, "allocation_pct": 60.0}],
                "builtin_strategies": [{"name": "Grid BOT", "allocation_pct": 40.0, "config": {}}],
            },
            session=session,
            user_id=1,
        )
    assert len(resolved) == 2
    assert resolved[0]["name"] == "Grid BOT"
    assert resolved[1]["name"] == "My Strategy"
    assert resolved[1]["weight"] == 60.0
    assert resolved[1]["config"]["strategy_type"] == "builder_vwap"


async def test_vwap_backtest_rejects_unknown_indicators() -> None:
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 140,
        "candles": _candles(),
        "enabled": ["VWAP", "NOT_A_REAL_INDICATOR"],
        "regime": "Bull",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/vwap", json=payload)
    assert response.status_code == 422


async def test_vwap_backtest_supports_extended_stop_and_sizing_contract() -> None:
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 140,
        "candles": _candles(),
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Bull",
        "stop_mode": "Swing",
        "swing_lookback": 15,
        "swing_buffer_atr": 0.2,
        "max_position_pct": 50.0,
        "include_series": False,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/vwap", json=payload)
    assert response.status_code == 200
    body = response.json()
    if body["trades"]:
        first_trade = body["trades"][0]
        assert first_trade["stop_mode"] == "Swing"
        assert isinstance(first_trade["sl_explain"], dict)
