from datetime import UTC, datetime, timedelta

import pytest

from app.services.backtesting.common import (
    CRYPTO_YEAR_DAYS,
    add_capital_metrics,
    build_r_cumulative_curve,
    calculate_annualized_return_pct,
    calculate_calmar_ratio,
    calculate_equity_max_drawdown_pct,
    calculate_performance_metrics,
    calculate_r_squared,
    calculate_sharpe_proxy,
    compute_trade_r_multiple,
)


def test_compute_trade_r_multiple_prefers_r_real() -> None:
    trade = {"r_real": 1.5, "pnl_usdt": 90.0, "risk_usdt": 30.0}
    assert compute_trade_r_multiple(trade) == 1.5


def test_compute_trade_r_multiple_uses_risk_usdt() -> None:
    trade = {"pnl_usdt": 45.0, "risk_usdt": 15.0}
    assert compute_trade_r_multiple(trade) == 3.0


def test_compute_trade_r_multiple_uses_entry_sl_and_position_size() -> None:
    trade = {"entry": 100.0, "sl": 95.0, "position_size": 2.0, "pnl_usdt": 15.0}
    assert compute_trade_r_multiple(trade) == 1.5


def test_compute_trade_r_multiple_uses_entry_sl_and_allocation() -> None:
    trade = {"entry": 100.0, "sl": 90.0, "allocation_usdt": 1000.0, "pnl_usdt": 150.0}
    assert compute_trade_r_multiple(trade) == 1.5


def test_compute_trade_r_multiple_returns_none_when_risk_unknown() -> None:
    trade = {"entry": 100.0, "pnl_usdt": 15.0}
    assert compute_trade_r_multiple(trade) is None


def test_build_r_cumulative_curve_returns_zero_prefixed_curve() -> None:
    assert build_r_cumulative_curve([1.0, -0.5, 2.0]) == [0.0, 1.0, 0.5, 2.5]


def test_calculate_r_squared_returns_one_for_perfect_line() -> None:
    r_squared = calculate_r_squared([0.0, 1.0, 2.0, 3.0, 4.0], valid_r_count=4)
    assert r_squared == pytest.approx(1.0)


def test_calculate_r_squared_returns_zero_for_insufficient_points() -> None:
    assert calculate_r_squared([0.0, 1.0], valid_r_count=1) == 0.0


def test_add_capital_metrics_enriches_r_fields_and_skips_undefined_risk() -> None:
    trades = [
        {"exit_reason": "TAKE", "pnl_usdt": 20.0, "risk_usdt": 10.0},
        {"exit_reason": "STOP", "entry": 100.0, "sl": 95.0, "position_size": 2.0, "pnl_usdt": 8.0},
        {
            "exit_reason": "TIME",
            "entry": 100.0,
            "sl": 90.0,
            "allocation_usdt": 1000.0,
            "pnl_usdt": 100.0,
        },
        {"exit_reason": "TAKE", "pnl_usdt": 30.0},
    ]

    summary, _ = add_capital_metrics(summary={}, trades=trades, initial_balance=1000.0)

    assert trades[0]["r_multiple"] == pytest.approx(2.0)
    assert trades[1]["r_multiple"] == pytest.approx(0.8)
    assert trades[2]["r_multiple"] == pytest.approx(1.0)
    assert trades[3]["r_multiple"] is None
    assert summary["r_cumulative"] == pytest.approx(3.8)
    assert summary["total_r"] == pytest.approx(3.8)
    assert summary["avg_r"] == pytest.approx((2.0 + 0.8 + 1.0) / 3.0)
    assert "max_drawdown" in summary
    assert "max_drawdown_pct" in summary
    assert "annualized_return_pct" in summary
    assert "calmar_ratio" in summary
    assert "sharpe_proxy" in summary
    assert "walk_forward_stability" in summary
    assert 0.0 <= float(summary["r_squared"]) <= 1.0


def test_calmar_metrics_use_equity_drawdown_and_crypto_year_period() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    trades = [
        {"exit_reason": "TAKE", "exit_time": start + timedelta(days=10), "pnl_usdt": 100.0},
        {"exit_reason": "STOP", "exit_time": start + timedelta(days=20), "pnl_usdt": -200.0},
        {"exit_reason": "TAKE", "exit_time": start + timedelta(days=30), "pnl_usdt": 300.0},
    ]

    summary, equity_curve = add_capital_metrics(
        summary={},
        trades=trades,
        initial_balance=1000.0,
        period_start=start,
        period_end=start + timedelta(days=CRYPTO_YEAR_DAYS / 2.0),
    )

    assert summary["total_return_pct"] == pytest.approx(20.0)
    assert summary["max_drawdown_pct"] == pytest.approx(200.0 / 1100.0 * 100.0)
    assert summary["annualized_return_pct"] == pytest.approx(44.0)
    assert summary["calmar_ratio"] == pytest.approx(44.0 / (200.0 / 1100.0 * 100.0))
    assert summary["client_values"]["calmarRatio"] == pytest.approx(summary["calmar_ratio"])
    assert calculate_equity_max_drawdown_pct(equity_curve) == pytest.approx(
        summary["max_drawdown_pct"]
    )


def test_calmar_metrics_are_json_safe_when_drawdown_or_final_balance_is_invalid() -> None:
    assert calculate_calmar_ratio(50.0, 0.0) == 0.0
    assert calculate_annualized_return_pct(
        initial_balance=1000.0,
        final_balance=0.0,
        period_days=CRYPTO_YEAR_DAYS,
    ) == 0.0


def test_calculate_sharpe_proxy_uses_sample_std_and_trade_count() -> None:
    values = [1.0, -0.5, 2.0]
    assert calculate_sharpe_proxy(values) == pytest.approx(1.1470786693528088)


def test_calculate_performance_metrics_keeps_profit_factor_json_safe() -> None:
    metrics = calculate_performance_metrics(
        [
            {"exit_reason": "TAKE", "pnl_usdt": 10.0, "risk_usdt": 10.0},
            {"exit_reason": "TAKE", "pnl_usdt": 20.0, "risk_usdt": 10.0},
        ]
    )

    assert metrics["profit_factor"] == pytest.approx(3.0)
