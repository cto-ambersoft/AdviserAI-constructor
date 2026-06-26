from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1.endpoints import backtest as backtest_endpoint
from app.core.config import get_settings
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


def _write_ai_forecast_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "signal_time_utc,predicted_trend,confidence_bull,confidence_bear,confidence_flat",
                "2025-01-01T00:00:00+00:00,bull,85.0,20.0,10.0",
                "2025-01-02T00:00:00+00:00,bear,35.0,90.0,15.0",
                "2025-01-03T00:00:00+00:00,flat,45.0,45.0,85.0",
            ]
        ),
        encoding="utf-8",
    )


async def test_metrics_schema_endpoint_returns_definition_and_flags_version() -> None:
    from app.services.backtesting.run_manifest import metric_formula_version

    current = metric_formula_version()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        plain = await client.get("/api/v1/backtest/metrics-schema")
        matched = await client.get(
            "/api/v1/backtest/metrics-schema", params={"version": current}
        )
        stale = await client.get(
            "/api/v1/backtest/metrics-schema", params={"version": "stale000000"}
        )

    assert plain.status_code == 200
    body = plain.json()
    assert body["metric_formula_version"] == current
    assert "metrics" in body["metrics_schema"]

    assert matched.json()["matches_current"] is True
    assert stale.json()["matches_current"] is False


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
    assert set(body.keys()) == {
        "summary",
        "trades",
        "chart_points",
        "explanations",
        "run_manifest",
    }
    assert body["run_manifest"]["engine"] == "vwap"
    assert "r_squared" in body["summary"]
    assert "r_cumulative" in body["summary"]
    assert "avg_r" in body["summary"]
    assert "total_r" in body["summary"]
    assert body["chart_points"] == {}


async def test_ai_forecast_files_endpoint_returns_sorted_csv_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    (exports_dir / "zeta.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (exports_dir / "alpha.csv").write_text("a,b\n3,4\n", encoding="utf-8")
    (exports_dir / "ignore.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(backtest_endpoint.service, "_exports_dir", exports_dir)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/backtest/ai-forecast-files")
    assert response.status_code == 200
    body = response.json()
    assert [item["file_name"] for item in body["files"]] == ["alpha.csv", "zeta.csv"]
    assert all("modified_at_utc" in item for item in body["files"])


async def test_ai_forecast_files_endpoint_uses_configured_exports_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exports_dir = tmp_path / "custom-exports"
    exports_dir.mkdir()
    (exports_dir / "forecast.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    monkeypatch.setenv("AI_FORECAST_EXPORTS_DIR", str(exports_dir))
    get_settings.cache_clear()
    try:
        fresh_service = BacktestingService()
        monkeypatch.setattr(backtest_endpoint, "service", fresh_service)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/backtest/ai-forecast-files")
        assert response.status_code == 200
        assert response.json()["files"][0]["file_name"] == "forecast.csv"
    finally:
        get_settings.cache_clear()


async def test_vwap_backtest_with_ai_returns_baseline_and_ai_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    csv_path = exports_dir / "forecast.csv"
    _write_ai_forecast_csv(csv_path)
    monkeypatch.setattr(backtest_endpoint.service, "_exports_dir", exports_dir)

    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 180,
        "candles": _candles(180),
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Flat",
        "run_with_ai": True,
        "ai_forecast_file": "forecast.csv",
        "ai_bull_confidence_threshold": 70.0,
        "ai_bear_confidence_threshold": 70.0,
        "include_series": False,
        "trades_limit": 100,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/vwap", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"result", "baseline", "comparison"}
    assert set(body["comparison"].keys()) == {
        "total_pnl_delta",
        "win_rate_delta",
        "trades_delta",
        "profit_factor_delta",
        "sharpe_proxy_delta",
        "max_drawdown_delta",
        "calmar_ratio_delta",
    }
    expected_keys = {"summary", "trades", "chart_points", "explanations", "run_manifest"}
    assert set(body["result"].keys()) == expected_keys
    assert set(body["baseline"].keys()) == expected_keys
    assert "r_squared" in body["result"]["summary"]
    assert "r_cumulative" in body["result"]["summary"]
    assert "calmar_ratio" in body["result"]["summary"]
    assert "r_squared" in body["baseline"]["summary"]
    assert "r_cumulative" in body["baseline"]["summary"]
    assert "calmar_ratio" in body["baseline"]["summary"]
    assert body["baseline"]["chart_points"] == {}
    assert body["result"]["chart_points"] == {}
    assert body["comparison"]["trades_delta"] == (
        int(body["result"]["summary"].get("total_trades", 0))
        - int(body["baseline"]["summary"].get("total_trades", 0))
    )


async def test_vwap_backtest_with_ai_accepts_null_thresholds_with_runtime_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    csv_path = exports_dir / "forecast.csv"
    _write_ai_forecast_csv(csv_path)
    monkeypatch.setattr(backtest_endpoint.service, "_exports_dir", exports_dir)

    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 180,
        "candles": _candles(180),
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Flat",
        "run_with_ai": True,
        "ai_forecast_file": "forecast.csv",
        "ai_bull_confidence_threshold": None,
        "ai_bear_confidence_threshold": None,
        "include_series": False,
        "trades_limit": 100,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/vwap", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"result", "baseline", "comparison"}


async def test_vwap_with_ai_baseline_matches_plain_run_and_keeps_user_candles() -> None:
    service = BacktestingService()
    candles = _candles(160)
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 160,
        "candles": candles,
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Flat",
        "include_series": True,
    }
    plain = await service.run_vwap({**payload, "run_with_ai": False})
    comparison = await service.run_vwap_with_ai_rows(
        {
            **payload,
            "run_with_ai": True,
            "ai_forecast_rows": [
                {
                    "signal_time_utc": "2025-03-01T00:00:00+00:00",
                    "horizon_end_utc": "2025-03-02T00:00:00+00:00",
                    "predicted_trend": "bull",
                    "confidence_bull": 90.0,
                    "confidence_bear": 5.0,
                    "confidence_flat": 5.0,
                }
            ],
        }
    )

    trade_keys = (
        "side",
        "entry_i",
        "exit_i",
        "entry_time",
        "exit_time",
        "entry",
        "sl",
        "tp",
        "exit",
        "exit_reason",
        "pnl_usdt",
        "pnl_pct",
        "regime",
    )
    assert comparison["baseline"]["summary"] == plain["summary"]
    assert [
        {key: trade.get(key) for key in trade_keys}
        for trade in comparison["baseline"]["trades"]
    ] == [{key: trade.get(key) for key in trade_keys} for trade in plain["trades"]]
    result_chart = comparison["ai_forecast"]["chart_points"]
    assert len(result_chart["ohlcv"]) == len(candles)
    assert len(result_chart["ai_forecast_overlay"]) == len(candles)
    assert all(point["applied"] is False for point in result_chart["ai_forecast_overlay"])
    assert (
        pd.to_datetime(result_chart["ohlcv"][0]["time"], utc=True).isoformat()
        == candles[0]["time"]
    )
    assert (
        pd.to_datetime(result_chart["ohlcv"][-1]["time"], utc=True).isoformat()
        == candles[-1]["time"]
    )


async def test_ai_backtest_keeps_requested_market_window() -> None:
    service = BacktestingService()
    captured: dict[str, object] = {}

    async def _fake_load_market_frame(**kwargs: object) -> pd.DataFrame:
        captured.update(kwargs)
        frame = pd.DataFrame(_candles(120))
        frame["time"] = pd.to_datetime(frame["time"], utc=True)
        return frame.set_index("time")[["open", "high", "low", "close", "volume"]].astype(float)

    service.load_market_frame = _fake_load_market_frame
    await service._load_market_frame_from_payload(
        {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "bars": 120,
            "start_time": "2025-01-01T00:00:00+00:00",
            "end_time": "2025-01-06T00:00:00+00:00",
            "run_with_ai": True,
            "ai_forecast_rows": [
                {
                    "signal_time_utc": "2025-03-01T01:00:00+00:00",
                    "horizon_end_utc": "2025-03-02T01:00:00+00:00",
                    "predicted_trend": "bull",
                    "confidence_bull": 90.0,
                    "confidence_bear": 5.0,
                    "confidence_flat": 5.0,
                },
                {
                    "signal_time_utc": "2025-03-03T01:00:00+00:00",
                    "horizon_end_utc": "2025-03-04T01:00:00+00:00",
                    "predicted_trend": "bear",
                    "confidence_bull": 5.0,
                    "confidence_bear": 90.0,
                    "confidence_flat": 5.0,
                },
            ],
        }
    )

    assert captured["timeframe"] == "1h"
    assert captured["bars"] == 120
    assert captured["start_time"] == "2025-01-01T00:00:00+00:00"
    assert captured["end_time"] == "2025-01-06T00:00:00+00:00"


async def test_ai_backtest_without_explicit_dates_does_not_use_forecast_window() -> None:
    service = BacktestingService()
    captured: dict[str, object] = {}

    async def _fake_load_market_frame(**kwargs: object) -> pd.DataFrame:
        captured.update(kwargs)
        frame = pd.DataFrame(_candles(120))
        frame["time"] = pd.to_datetime(frame["time"], utc=True)
        return frame.set_index("time")[["open", "high", "low", "close", "volume"]].astype(float)

    service.load_market_frame = _fake_load_market_frame
    await service._load_market_frame_from_payload(
        {
            "symbol": "BTC/USDT",
            "timeframe": "4h",
            "bars": 120,
            "run_with_ai": True,
            "ai_forecast_rows": [
                {
                    "signal_time_utc": "2025-03-01T01:00:00+00:00",
                    "horizon_end_utc": "2025-03-02T01:00:00+00:00",
                    "predicted_trend": "bull",
                    "confidence_bull": 90.0,
                    "confidence_bear": 5.0,
                    "confidence_flat": 5.0,
                },
            ],
        }
    )

    assert captured["timeframe"] == "4h"
    assert captured["bars"] == 120
    assert "start_time" not in captured
    assert "end_time" not in captured


async def test_internal_compare_endpoint_returns_extended_delta_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal")
    get_settings.cache_clear()
    payload = {
        "strategy": "vwap",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 180,
        "data_config": {
            "candles": _candles(180),
        },
        "algo_config": {
            "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
            "regime": "Flat",
            "ai_bull_confidence_threshold": 70.0,
            "ai_bear_confidence_threshold": 70.0,
        },
        "ai_forecast_rows": [
            {
                "signal_time_utc": "2025-01-01T00:00:00+00:00",
                "predicted_trend": "bull",
                "confidence_bull": 90.0,
                "confidence_bear": 5.0,
                "confidence_flat": 5.0,
            }
        ],
    }
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/internal/backtest/compare",
                json=payload,
                headers={"X-Internal-API-Key": "test-internal"},
            )
        assert response.status_code == 200
        comparison = response.json()["comparison"]
        assert set(comparison.keys()) == {
            "total_pnl_delta",
            "win_rate_delta",
            "trades_delta",
            "profit_factor_delta",
            "sharpe_proxy_delta",
            "max_drawdown_delta",
            "calmar_ratio_delta",
        }
    finally:
        get_settings.cache_clear()


async def test_internal_compare_endpoint_includes_core_metrics_for_non_vwap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal")
    get_settings.cache_clear()
    payload = {
        "strategy": "intraday-momentum",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 220,
        "data_config": {
            "candles": _candles(220),
        },
        "algo_config": {
            "side": "long",
            "ai_bull_confidence_threshold": 70.0,
            "ai_bear_confidence_threshold": 70.0,
        },
        "ai_forecast_rows": [
            {
                "signal_time_utc": "2025-01-01T00:00:00+00:00",
                "predicted_trend": "bull",
                "confidence_bull": 90.0,
                "confidence_bear": 5.0,
                "confidence_flat": 5.0,
            }
        ],
    }
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/internal/backtest/compare",
                json=payload,
                headers={"X-Internal-API-Key": "test-internal"},
            )
        assert response.status_code == 200
        body = response.json()
        for summary in [body["result"]["summary"], body["baseline"]["summary"]]:
            assert "win_rate" in summary
            assert "max_drawdown" in summary
            assert "max_drawdown_pct" in summary
            assert "annualized_return_pct" in summary
            assert "calmar_ratio" in summary
            assert "sharpe_proxy" in summary
            assert "walk_forward_stability" in summary
        assert "max_drawdown_delta" in body["comparison"]
        assert "sharpe_proxy_delta" in body["comparison"]
        assert "calmar_ratio_delta" in body["comparison"]
    finally:
        get_settings.cache_clear()


async def test_vwap_backtest_with_ai_requires_file_name() -> None:
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 140,
        "candles": _candles(),
        "enabled": ["VWAP", "MACD"],
        "regime": "Bull",
        "run_with_ai": True,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/vwap", json=payload)
    assert response.status_code == 422


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
    assert "run_with_ai" in body["portfolio"]["builtin_strategy_params"]["VWAP Builder"]


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
    assert "r_cumulative_curve" in body["chart_points"]
    assert "r_equity_curve" in body["chart_points"]
    assert body["summary"]["final_equity"] == 5060.0
    assert body["summary"]["r_squared"] == 0.0
    assert body["summary"]["r_cumulative"] == 0.0
    assert body["summary"]["calmar_ratio"] > 0.0
    assert "client_values" in body["summary"]
    assert body["summary"]["client_values"]["finalEquity"] == 5060.0
    assert body["summary"]["client_values"]["calmarRatio"] == body["summary"]["calmar_ratio"]


async def test_portfolio_backtest_with_ai_returns_comparison_shape() -> None:
    payload = {
        "total_capital": 5000,
        "run_with_ai": True,
        "ai_forecast_rows": [
            {
                "signal_time_utc": "2025-01-01T00:00:00+00:00",
                "predicted_trend": "bull",
                "confidence_bull": 90.0,
                "confidence_bear": 5.0,
                "confidence_flat": 5.0,
            }
        ],
        "strategies": [
            {
                "name": "s1",
                "trades": [
                    {"exit_time": "2025-01-01T00:00:00+00:00", "pnl_usdt": 100},
                ],
            }
        ],
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/portfolio", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"result", "baseline", "comparison"}
    assert body["result"]["summary"]["final_equity"] == 5100.0
    assert body["baseline"]["summary"]["final_equity"] == 5100.0
    assert body["comparison"]["total_pnl_delta"] == 0.0


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


async def test_portfolio_backtest_async_accepts_user_strategies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_kiq(payload: dict[str, object]) -> SimpleNamespace:
        assert payload["async_job"] is True
        assert payload["user_strategies"] == [{"strategy_id": 7, "allocation_pct": 100.0}]
        return SimpleNamespace(task_id="task-123")

    monkeypatch.setattr(backtest_endpoint.run_portfolio_backtest, "kiq", _fake_kiq)

    payload = {
        "total_capital": 10_000,
        "user_strategies": [{"strategy_id": 7, "allocation_pct": 100.0}],
        "async_job": True,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/backtest/portfolio", json=payload)
    assert response.status_code == 200
    assert response.json() == {"status": "queued", "task_id": "task-123"}


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


async def test_portfolio_service_runs_saved_vwap_strategy_with_ai_forecast(
    backtest_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with backtest_db() as session:
        row = Strategy(
            user_id=1,
            name="My AI VWAP",
            strategy_type="builder_vwap",
            config={
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "bars": 140,
                "enabled": ["VWAP", "MACD"],
                "run_with_ai": True,
                "ai_forecast_file": "forecast.csv",
            },
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        captured: dict[str, object] = {}

        async def _fake_run_vwap_with_ai(self, payload: dict[str, object]) -> dict[str, object]:
            captured["payload"] = payload
            return {
                "baseline": {"summary": {}, "trades": [], "chart_points": {}, "explanations": []},
                "ai_forecast": {
                    "summary": {},
                    "trades": [
                        {
                            "exit_time": "2025-01-01T00:00:00+00:00",
                            "pnl_usdt": 125.0,
                        }
                    ],
                    "chart_points": {},
                    "explanations": [],
                },
            }

        monkeypatch.setattr(
            "app.services.backtesting.service.BacktestingService.run_vwap_with_ai",
            _fake_run_vwap_with_ai,
        )

        async def _fail_run_vwap(self, payload: dict[str, object]) -> dict[str, object]:
            raise AssertionError("Expected AI run path for run_with_ai strategy")

        monkeypatch.setattr(
            "app.services.backtesting.service.BacktestingService.run_vwap",
            _fail_run_vwap,
        )

        service = BacktestingService()
        result = await service.run_portfolio(
            {
                "total_capital": 10_000,
                "user_id": 1,
                "user_strategies": [{"strategy_id": row.id, "allocation_pct": 100.0}],
                "session": session,
            }
        )

    assert captured["payload"]["run_with_ai"] is True
    assert result["summary"]["total_events"] == 1
    assert result["summary"]["final_equity"] == 10_125.0
    assert result["trades"][0]["pnl_usdt"] == 125.0


async def test_portfolio_service_applies_top_level_ai_forecast_to_saved_strategy(
    backtest_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with backtest_db() as session:
        row = Strategy(
            user_id=1,
            name="My Plain VWAP",
            strategy_type="builder_vwap",
            config={
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "bars": 140,
                "enabled": ["VWAP", "MACD"],
            },
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        captured: dict[str, object] = {}

        async def _fake_run_vwap_with_ai(self, payload: dict[str, object]) -> dict[str, object]:
            captured["payload"] = payload
            return {
                "baseline": {"summary": {}, "trades": [], "chart_points": {}, "explanations": []},
                "ai_forecast": {
                    "summary": {},
                    "trades": [
                        {
                            "exit_time": "2025-01-01T00:00:00+00:00",
                            "pnl_usdt": 75.0,
                            "ai_forecast_applied": True,
                            "ai_regime": "Bull",
                        }
                    ],
                    "chart_points": {},
                    "explanations": [],
                },
            }

        monkeypatch.setattr(
            "app.services.backtesting.service.BacktestingService.run_vwap_with_ai",
            _fake_run_vwap_with_ai,
        )

        async def _fake_run_vwap(self, payload: dict[str, object]) -> dict[str, object]:
            assert payload["run_with_ai"] is False
            return {
                "summary": {},
                "trades": [
                    {
                        "exit_time": "2025-01-01T00:00:00+00:00",
                        "pnl_usdt": 50.0,
                    }
                ],
                "chart_points": {},
                "explanations": [],
            }

        monkeypatch.setattr(
            "app.services.backtesting.service.BacktestingService.run_vwap",
            _fake_run_vwap,
        )

        ai_rows = [
            {
                "signal_time_utc": "2025-01-01T00:00:00+00:00",
                "predicted_trend": "bull",
                "confidence_bull": 90.0,
                "confidence_bear": 5.0,
                "confidence_flat": 5.0,
            }
        ]
        service = BacktestingService()
        result = await service.run_portfolio(
            {
                "total_capital": 10_000,
                "user_id": 1,
                "user_strategies": [{"strategy_id": row.id, "allocation_pct": 100.0}],
                "session": session,
                "run_with_ai": True,
                "ai_forecast_rows": ai_rows,
                "ai_bull_confidence_threshold": 65.0,
                "ai_bear_confidence_threshold": 60.0,
            }
        )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["run_with_ai"] is True
    assert payload["ai_forecast_rows"] == ai_rows
    assert payload["ai_bull_confidence_threshold"] == 65.0
    assert payload["ai_bear_confidence_threshold"] == 60.0
    assert set(result.keys()) == {"result", "baseline", "comparison"}
    assert result["baseline"]["summary"]["final_equity"] == 10_050.0
    assert result["result"]["summary"]["final_equity"] == 10_075.0
    assert result["comparison"]["total_pnl_delta"] == 25.0
    assert result["result"]["summary"]["ai_forecast_applied"] is True
    assert result["result"]["summary"]["ai_forecast_events"] == 1
    assert result["result"]["trades"][0]["ai_forecast_applied"] is True
    assert result["result"]["trades"][0]["ai_regime"] == "Bull"


async def test_portfolio_service_resolves_user_strategies_without_session(
    backtest_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with backtest_db() as session:
        row = Strategy(
            user_id=1,
            name="My VWAP",
            strategy_type="builder_vwap",
            config={
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "bars": 140,
                "enabled": ["VWAP", "MACD"],
            },
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

    monkeypatch.setattr("app.services.backtesting.service.AsyncSessionFactory", backtest_db)

    async def _fake_run_vwap(self, payload: dict[str, object]) -> dict[str, object]:
        return {
            "summary": {},
            "trades": [{"exit_time": "2025-01-01T00:00:00+00:00", "pnl_usdt": 50.0}],
            "chart_points": {},
            "explanations": [],
        }

    monkeypatch.setattr(
        "app.services.backtesting.service.BacktestingService.run_vwap",
        _fake_run_vwap,
    )

    service = BacktestingService()
    result = await service.run_portfolio(
        {
            "total_capital": 5_000,
            "user_id": 1,
            "user_strategies": [{"strategy_id": row.id, "allocation_pct": 100.0}],
        }
    )

    assert result["summary"]["total_events"] == 1
    assert result["summary"]["final_equity"] == 5_050.0
    assert result["trades"][0]["pnl_usdt"] == 50.0


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


async def test_vwap_backtest_applies_trading_costs() -> None:
    """A2 (finding 7.4): VWAP nets trading costs off P&L; zero cost reproduces the
    no-fee baseline; costs change only P&L, not which trades were taken."""
    service = BacktestingService()
    base_payload: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 180,
        "candles": _candles(180),
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Bull",
        "account_balance": 1000.0,
        "trades_limit": 200,
    }
    baseline = await service.run_vwap({**base_payload, "fee_pct": 0.0})
    with_fee = await service.run_vwap({**base_payload, "fee_pct": 1.0})

    # Costs must not change which trades the strategy took.
    assert with_fee["summary"]["total_trades"] == baseline["summary"]["total_trades"]
    closed = [t for t in with_fee["trades"] if t.get("exit_reason") != "OPEN"]
    assert closed, "expected the trending candles to produce closed trades"
    # Costs are netted: the account ends lower with fees, each closed trade has a cost.
    assert with_fee["summary"]["final_balance"] < baseline["summary"]["final_balance"]
    assert all(float(t.get("cost_usdt", 0.0)) > 0 for t in closed)
