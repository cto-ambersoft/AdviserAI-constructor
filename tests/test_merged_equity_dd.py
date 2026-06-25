"""T12 (W11a): true portfolio drawdown is the max drawdown of ONE equity curve
built from every strategy's closed trades (time-ordered), not the worst single
strategy's drawdown (the old proxy). This catches portfolio-wide bleed that no
individual strategy breaches.
"""

from app.services.auto_trade.portfolio import merged_equity_max_dd_pct


def test_zero_when_no_trades_or_no_base() -> None:
    assert merged_equity_max_dd_pct([], 100.0) == 0.0
    assert merged_equity_max_dd_pct([{"pnl_usdt": -5, "exit_time": "t1"}], 0.0) == 0.0


def test_drawdown_over_combined_time_ordered_curve() -> None:
    # base 100 → equity 100 →(+10) 110 →(-5) 105 →(-5) 100. Peak 110, trough 100.
    # max DD = (110-100)/110 = 9.0909...%
    trades = [
        {"pnl_usdt": -5, "exit_time": "2026-06-01T03:00:00"},
        {"pnl_usdt": 10, "exit_time": "2026-06-01T01:00:00"},
        {"pnl_usdt": -5, "exit_time": "2026-06-01T02:00:00"},
    ]
    dd = merged_equity_max_dd_pct(trades, 100.0)
    assert round(dd, 2) == 9.09


def test_captures_portfolio_bleed_two_strategies_neither_breaches_alone() -> None:
    # Strategy A alone: +6 then +0 → no drawdown. Strategy B alone: +0 then -10 →
    # its own DD is modest. Merged & time-ordered the combined equity peaks then
    # bleeds across BOTH, producing a real portfolio drawdown the worst-strategy
    # proxy (max of the per-strategy DDs) would understate.
    strat_a = [{"pnl_usdt": 6, "exit_time": "2026-06-01T01:00:00"}]
    strat_b = [
        {"pnl_usdt": 4, "exit_time": "2026-06-01T02:00:00"},
        {"pnl_usdt": -10, "exit_time": "2026-06-01T03:00:00"},
    ]
    dd = merged_equity_max_dd_pct(strat_a + strat_b, 100.0)
    # peak 110 (after +6,+4), trough 100 → 10/110 = 9.09%
    assert round(dd, 2) == 9.09
