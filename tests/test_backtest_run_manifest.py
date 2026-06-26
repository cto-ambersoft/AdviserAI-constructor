"""Finding 7.1/7.2/7.3 — backtest run manifest. The manifest pins the exact
computation behind a result: a content hash of the metric-formula code, the
engine version, the cost model, a hash of the OHLCV window and the data window.

AC: identical metric code + identical candles always yield identical
``metric_formula_version`` / ``candles_hash`` (deterministic, version-stable)."""

from datetime import UTC, datetime, timedelta

import pandas as pd

from app.services.backtesting.cost_model import CostModel
from app.services.backtesting.run_manifest import (
    ENGINE_VERSION,
    build_metric_formula_definition,
    build_run_manifest,
    candles_hash,
    metric_formula_version,
)
from app.services.backtesting.service import BacktestingService
from app.services.market_data.service import MarketDataService


def _frame(close: list[float] | None = None) -> pd.DataFrame:
    idx = pd.to_datetime(
        ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z"]
    )
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": close or [100.5, 101.5, 102.5],
            "volume": [10.0, 11.0, 12.0],
        },
        index=idx,
    )


def test_metric_formula_version_is_stable_sha256() -> None:
    v1 = metric_formula_version()
    v2 = metric_formula_version()
    assert v1 == v2
    assert len(v1) == 64
    int(v1, 16)  # parses as hex


def test_candles_hash_is_stable_for_same_input() -> None:
    assert candles_hash(_frame()) == candles_hash(_frame())
    assert len(candles_hash(_frame())) == 64


def test_candles_hash_changes_when_ohlcv_changes() -> None:
    assert candles_hash(_frame()) != candles_hash(_frame(close=[100.5, 101.5, 999.0]))


def test_build_run_manifest_contains_versions_cost_and_window() -> None:
    cost = CostModel(fee_pct=0.06, slippage_pct=0.01, funding_pct_per_bar=0.0)
    manifest = build_run_manifest(engine="vwap", candles=_frame(), cost=cost)

    assert manifest["engine"] == "vwap"
    assert manifest["engine_version"] == ENGINE_VERSION
    assert manifest["metric_formula_version"] == metric_formula_version()
    assert manifest["candles_hash"] == candles_hash(_frame())
    assert manifest["cost_model"] == {
        "fee_pct": 0.06,
        "slippage_pct": 0.01,
        "funding_pct_per_bar": 0.0,
    }
    assert manifest["data_window"]["bars"] == 3
    assert manifest["data_window"]["start"] == "2026-01-01T00:00:00+00:00"
    assert manifest["data_window"]["end"] == "2026-01-01T02:00:00+00:00"
    assert "env" in manifest


def test_build_run_manifest_is_deterministic_for_same_input() -> None:
    cost = CostModel(fee_pct=0.06)
    m1 = build_run_manifest(engine="vwap", candles=_frame(), cost=cost, seed=42)
    m2 = build_run_manifest(engine="vwap", candles=_frame(), cost=cost, seed=42)

    assert m1["metric_formula_version"] == m2["metric_formula_version"]
    assert m1["candles_hash"] == m2["candles_hash"]
    assert m1["seed"] == 42


def test_metric_formula_definition_carries_version_and_schema() -> None:
    definition = build_metric_formula_definition()
    assert definition["metric_formula_version"] == metric_formula_version()
    assert definition["engine_version"] == ENGINE_VERSION
    schema = definition["metrics_schema"]
    assert "groups" in schema and "metrics" in schema
    # every metric carries its human-readable formula intent (description)
    assert all("description" in metric for metric in schema["metrics"])


def test_metric_formula_definition_flags_version_match() -> None:
    current = metric_formula_version()
    matched = build_metric_formula_definition(requested_version=current)
    assert matched["requested_version"] == current
    assert matched["matches_current"] is True

    stale = build_metric_formula_definition(requested_version="stale000000")
    assert stale["matches_current"] is False


def _candle_rows(count: int = 240) -> list[dict[str, float | str]]:
    base = datetime(2025, 1, 1, tzinfo=UTC)
    rows: list[dict[str, float | str]] = []
    price = 100.0
    for i in range(count):
        drift = 0.4 if (i % 20) < 10 else -0.5
        op = price
        cp = price + drift
        rows.append(
            {
                "time": (base + timedelta(hours=i)).isoformat(),
                "open": op,
                "high": max(op, cp) + 0.6,
                "low": min(op, cp) - 0.6,
                "close": cp,
                "volume": 1000.0 + (i % 30) * 10,
            }
        )
        price = cp
    return rows


async def test_run_vwap_response_carries_run_manifest() -> None:
    rows = _candle_rows()
    payload: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": len(rows),
        "candles": rows,
        "regime": "Flat",
        "fee_pct": 0.06,
    }
    service = BacktestingService()
    result = await service.run_vwap(payload)

    manifest = result["run_manifest"]
    assert manifest["engine"] == "vwap"
    assert manifest["engine_version"] == ENGINE_VERSION
    assert manifest["metric_formula_version"] == metric_formula_version()
    # The hash pins the exact OHLCV window the engine actually ran on.
    df = MarketDataService.frame_from_candles(rows)
    assert manifest["candles_hash"] == candles_hash(df)
    assert manifest["cost_model"]["fee_pct"] == 0.06
    assert manifest["data_window"]["bars"] == len(rows)
