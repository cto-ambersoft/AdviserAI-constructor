"""Finding 7.4 — shared backtest cost model: fees/slippage/funding netted off P&L
uniformly. Pure, deterministic; a zero cost model is a no-op (so existing numbers
are unchanged when costs are off)."""

import pytest

from app.services.backtesting.cost_model import (
    CostModel,
    apply_cost_model,
    refresh_net_pnl_summary,
    trade_cost_usdt,
)


def test_trade_cost_usdt_round_trip_fee() -> None:
    # 0.06% per side on 1000 entry + 1000 exit notional = (2000) * 0.0006 = 1.2
    cost = CostModel(fee_pct=0.06)
    assert trade_cost_usdt(
        entry_notional=1000.0, exit_notional=1000.0, holding_bars=0.0, cost=cost
    ) == 1.2


def test_trade_cost_usdt_includes_slippage_and_funding() -> None:
    cost = CostModel(fee_pct=0.0, slippage_pct=0.0, funding_pct_per_bar=0.01)
    # funding only: 1000 * 0.0001 * 5 bars = 0.5
    assert trade_cost_usdt(
        entry_notional=1000.0, exit_notional=1000.0, holding_bars=5.0, cost=cost
    ) == 0.5


def test_apply_cost_model_nets_pnl_and_records_cost() -> None:
    trades = [
        {
            "entry_notional": 1000.0,
            "exit_notional": 1000.0,
            "pnl_usdt": 50.0,
            "exit_reason": "TAKE",
        },
    ]
    out = apply_cost_model(trades, CostModel(fee_pct=0.06))
    assert out[0]["cost_usdt"] == 1.2
    assert out[0]["pnl_usdt"] == 48.8


def test_apply_cost_model_preserves_pnl_pct_basis() -> None:
    # I1: pnl_pct must keep the engine's own basis (here a fraction-of-capital
    # value, NOT percent of entry notional). It scales by net/gross, not to a
    # fixed net/entry_notional*100.
    trades = [
        {
            "entry_notional": 1000.0,
            "exit_notional": 1000.0,
            "pnl_usdt": 50.0,
            "pnl_pct": 0.05,  # fraction-of-total-capital basis (e.g. Grid)
            "exit_reason": "TAKE",
        }
    ]
    out = apply_cost_model(trades, CostModel(fee_pct=0.06))
    # net = 48.8; pnl_pct scales 0.05 * (48.8 / 50.0) = 0.0488 — NOT 4.88
    assert out[0]["pnl_pct"] == pytest.approx(0.05 * 48.8 / 50.0)


def test_apply_cost_model_leaves_pnl_pct_when_gross_pnl_is_zero() -> None:
    trades = [
        {
            "entry_notional": 1000.0,
            "exit_notional": 1000.0,
            "pnl_usdt": 0.0,
            "pnl_pct": 0.0,
            "exit_reason": "TAKE",
        }
    ]
    out = apply_cost_model(trades, CostModel(fee_pct=0.06))
    assert out[0]["pnl_pct"] == 0.0  # cannot scale a zero gross; left as-is
    assert out[0]["pnl_usdt"] == pytest.approx(-1.2)  # still nets the cost


def test_refresh_net_pnl_summary_recomputes_from_net_trades() -> None:
    # I2: win_rate + total_pnl_usdt must come from the NET (post-cost) trades, so a
    # winner that flips to a loser after fees is no longer counted as a win.
    summary = {"win_rate": 100.0, "total_pnl_usdt": 999.0}  # stale / gross
    trades = [
        {"pnl_usdt": 5.0, "exit_reason": "TAKE"},
        {"pnl_usdt": -2.0, "exit_reason": "STOP"},
        {"pnl_usdt": -1.0, "exit_reason": "STOP"},
        {"pnl_usdt": 0.0, "exit_reason": "OPEN"},  # open trade ignored
    ]
    refresh_net_pnl_summary(summary, trades)
    assert summary["total_pnl_usdt"] == pytest.approx(2.0)  # 5 - 2 - 1
    assert summary["win_rate"] == pytest.approx(1 / 3 * 100)  # 1 win of 3 closed


def test_zero_cost_is_noop() -> None:
    trades = [
        {
            "entry_notional": 1000.0,
            "exit_notional": 1000.0,
            "pnl_usdt": 50.0,
            "exit_reason": "TAKE",
        }
    ]
    out = apply_cost_model(trades, CostModel())
    assert out[0]["pnl_usdt"] == 50.0
    assert "cost_usdt" not in out[0]  # untouched


def test_open_trades_are_skipped() -> None:
    trades = [{"entry_notional": 1000.0, "pnl_usdt": 0.0, "exit_reason": "OPEN"}]
    out = apply_cost_model(trades, CostModel(fee_pct=0.06))
    assert "cost_usdt" not in out[0]
    assert out[0]["pnl_usdt"] == 0.0


def test_notional_derived_from_qty_and_price() -> None:
    # no explicit notional: qty * entry = 10 * 100 = 1000 each side
    trades = [
        {
            "qty": 10.0,
            "entry": 100.0,
            "exit_price": 100.0,
            "pnl_usdt": 0.0,
            "exit_reason": "TAKE",
        }
    ]
    out = apply_cost_model(trades, CostModel(fee_pct=0.06))
    assert out[0]["cost_usdt"] == 1.2  # (1000 + 1000) * 0.0006
