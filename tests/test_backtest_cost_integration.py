"""Finding 7.4 — every backtest engine threads the shared cost model with the
caller's cost params. The cost math itself is unit-tested in test_backtest_cost_model;
here we verify the wiring per engine (the params flow into apply_cost_model), which
is robust to whether a given candle fixture happens to produce trades."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.services.backtesting.atr_order_block as atr_mod
import app.services.backtesting.grid_bot as grid_mod
import app.services.backtesting.intraday_momentum as intraday_mod
import app.services.backtesting.knife_catcher as knife_mod
from app.services.market_data.service import MarketDataService


def _frame(count: int = 240):
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
    return MarketDataService.frame_from_candles(rows)


def _spy_cost(monkeypatch: pytest.MonkeyPatch, module: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    real = module.apply_cost_model

    def spy(trades: Any, cost: Any) -> Any:
        captured["cost"] = cost
        return real(trades, cost)

    monkeypatch.setattr(module, "apply_cost_model", spy)
    return captured


def test_atr_engine_threads_cost_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _spy_cost(monkeypatch, atr_mod)
    atr_mod.run_atr_order_block(_frame(), {"allocation_usdt": 1000.0, "fee_pct": 0.5})
    assert captured["cost"].fee_pct == 0.5


def test_knife_engine_threads_cost_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _spy_cost(monkeypatch, knife_mod)
    knife_mod.run_knife_catcher(_frame(), {"account_balance": 1000.0, "fee_pct": 0.5})
    assert captured["cost"].fee_pct == 0.5


def test_grid_engine_threads_cost_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _spy_cost(monkeypatch, grid_mod)
    grid_mod.run_grid_bot(_frame(), {"order_fee_pct": 0.5})
    assert captured["cost"].fee_pct == 0.5  # legacy order_fee_pct maps to fee_pct


def test_intraday_engine_threads_cost_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _spy_cost(monkeypatch, intraday_mod)
    intraday_mod.run_intraday_momentum(_frame(), {"fee_pct": 0.5})
    assert captured["cost"].fee_pct == 0.5


def test_grid_unification_reproduces_inline_fee_pnl() -> None:
    """A4 regression: routing Grid through the shared cost model must reproduce the
    same net P&L its inline fee math produced (the cost-model fee formula is
    identical: (entry*qty + exit*qty) * fee)."""
    result = grid_mod.run_grid_bot(_frame(300), {"order_fee_pct": 0.06})
    assert result["summary"]["total_trades"] == 421
    assert result["summary"]["total_pnl"] == pytest.approx(177.502897, abs=1e-4)
    # net P&L field stays consistent after costs (I2)
    assert result["summary"]["total_pnl_usdt"] == pytest.approx(
        result["summary"]["total_pnl"], abs=1e-9
    )
    closed = [t for t in result["trades"] if t.get("exit_reason") != "OPEN"]
    # I2: win_rate is computed from NET pnl_usdt (not a gross pre-summary).
    net_wins = sum(1 for t in closed if float(t["pnl_usdt"]) > 0)
    assert result["summary"]["win_rate"] == pytest.approx(net_wins / len(closed) * 100.0)
    # I1: per-trade pnl_pct keeps Grid's basis (pnl as a fraction of total capital,
    # initial_capital=1000), not net/entry_notional*100.
    sample = closed[0]
    assert float(sample["pnl_pct"]) == pytest.approx(float(sample["pnl_usdt"]) / 1000.0, rel=1e-6)
