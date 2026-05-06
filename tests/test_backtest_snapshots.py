from datetime import UTC, datetime, timedelta

import pandas as pd

from app.services.backtesting.atr_order_block import run_atr_order_block
from app.services.backtesting.grid_bot import run_grid_bot
from app.services.backtesting.intraday_momentum import run_intraday_momentum
from app.services.backtesting.service import BacktestingService
from app.services.backtesting.vwap_builder import (
    resolve_ai_forecast_overlay_per_bar,
    resolve_ai_regimes_per_bar,
    run_vwap_backtest,
)
from app.services.indicators.engine import calc_indicators
from app.services.market_data.service import MarketDataService


def _assert_capital_metrics(result: dict[str, object]) -> None:
    summary = result["summary"]
    chart_points = result["chart_points"]
    assert isinstance(summary, dict)
    assert "initial_balance" in summary
    assert "final_balance" in summary
    assert "total_pnl" in summary
    assert "avg_risk_per_trade" in summary
    assert "avg_r" in summary
    assert "total_r" in summary
    assert "r_cumulative" in summary
    assert "r_squared" in summary
    assert "max_drawdown" in summary
    assert "max_drawdown_pct" in summary
    assert "annualized_return_pct" in summary
    assert "calmar_ratio" in summary
    assert "sharpe_proxy" in summary
    assert "walk_forward_stability" in summary
    assert "client_values" in summary
    assert "client_labels" in summary
    assert "client_stats" in summary
    assert isinstance(summary["client_values"], dict)
    assert isinstance(summary["client_labels"], dict)
    assert isinstance(summary["client_stats"], list)
    assert "calmarRatio" in summary["client_values"]
    assert "equity_curve" in chart_points
    assert "r_cumulative_curve" in chart_points
    assert "r_equity_curve" in chart_points
    assert isinstance(chart_points["equity_curve"], list)
    assert isinstance(chart_points["r_cumulative_curve"], list)
    assert isinstance(chart_points["r_equity_curve"], list)
    assert len(chart_points["equity_curve"]) >= 1
    assert len(chart_points["r_cumulative_curve"]) >= 1
    assert len(chart_points["r_equity_curve"]) >= 1


def _assert_trade_confirmation_shape(trade: dict[str, object]) -> None:
    assert "confirmation_status" in trade
    assert "outcome" in trade
    assert "is_take_profit" in trade
    assert "is_stop_loss" in trade
    assert "is_closed" in trade
    assert "is_profit" in trade
    assert "exit_reason_normalized" in trade
    assert "entryIndex" in trade
    assert "exitIndex" in trade
    assert "entryTime" in trade
    assert "exitTime" in trade
    assert "entryPrice" in trade
    assert "r_multiple" in trade


def _frame(count: int = 220):
    base = datetime(2025, 1, 1, tzinfo=UTC)
    rows: list[dict[str, float | str]] = []
    price = 100.0
    for i in range(count):
        drift = 0.15 if (i % 20) < 10 else -0.1
        open_price = price
        close_price = price + drift
        high = max(open_price, close_price) + 0.4
        low = min(open_price, close_price) - 0.4
        rows.append(
            {
                "time": (base + timedelta(hours=i)).isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": 1000 + (i % 30) * 10,
            }
        )
        price = close_price
    return MarketDataService.frame_from_candles(rows)


def test_vwap_snapshot_summary_trades_explanations() -> None:
    df = _frame()
    payload = {
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Flat",
        "rr": 2.0,
        "atr_mult": 1.5,
        "stop_mode": "ATR",
        "account_balance": 1000.0,
        "risk_per_trade": 1.0,
        "max_positions": 2,
        "max_position_pct": 100.0,
    }
    result = run_vwap_backtest(df, calc_indicators(df), payload)
    assert set(result.keys()) == {"summary", "trades", "chart_points", "explanations"}
    assert isinstance(result["summary"]["total_trades"], int)
    _assert_capital_metrics(result)
    if result["trades"]:
        assert "sl_explain" in result["trades"][0]
        _assert_trade_confirmation_shape(result["trades"][0])
    if result["explanations"]:
        assert "sl_explain" in result["explanations"][0]


def test_vwap_snapshot_with_ai_forecast_supports_dynamic_regime() -> None:
    df = _frame()
    payload = {
        "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
        "regime": "Bull",
        "rr": 2.0,
        "atr_mult": 1.5,
        "stop_mode": "ATR",
        "account_balance": 1000.0,
        "risk_per_trade": 1.0,
        "max_positions": 2,
        "max_position_pct": 100.0,
        "run_with_ai": True,
        "ai_bull_confidence_threshold": 70.0,
        "ai_bear_confidence_threshold": 70.0,
        "ai_forecast_rows": [
            {
                "signal_time_utc": "2025-01-01T00:00:00+00:00",
                "predicted_trend": "bull",
                "confidence_bull": 85.0,
                "confidence_bear": 20.0,
                "confidence_flat": 10.0,
            },
            {
                "signal_time_utc": "2025-01-03T00:00:00+00:00",
                "predicted_trend": "bear",
                "confidence_bull": 30.0,
                "confidence_bear": 88.0,
                "confidence_flat": 12.0,
            },
        ],
    }
    result = run_vwap_backtest(df, calc_indicators(df), payload)
    assert set(result.keys()) == {"summary", "trades", "chart_points", "explanations"}
    _assert_capital_metrics(result)
    assert result["summary"]["ai_forecast_applied"] is True
    assert set(result["summary"]["ai_regime_counts"]) == {"Bull", "Flat", "Bear"}
    assert result["explanations"][-1]["type"] == "ai_forecast"
    if result["trades"]:
        regimes = {trade.get("regime") for trade in result["trades"]}
        assert regimes <= {"Bull", "Flat", "Bear"}
        assert result["trades"][0]["ai_forecast_applied"] is True
        assert result["trades"][0]["ai_regime"] in {"Bull", "Flat", "Bear"}


def test_resolve_ai_regimes_falls_back_before_first_signal() -> None:
    market_index = pd.to_datetime(
        [
            "2025-01-01T00:00:00+00:00",
            "2025-01-01T01:00:00+00:00",
            "2025-01-01T02:00:00+00:00",
            "2025-01-01T03:00:00+00:00",
        ],
        utc=True,
    )
    ai_rows = [
        {
            "signal_time_utc": "2025-01-01T02:00:00+00:00",
            "predicted_trend": "bull",
            "confidence_bull": 80.0,
            "confidence_bear": 10.0,
            "confidence_flat": 20.0,
        }
    ]
    resolved = resolve_ai_regimes_per_bar(
        market_index=market_index,
        ai_rows=ai_rows,
        fallback_regime="Bear",
        bull_threshold=70.0,
        bear_threshold=70.0,
    )
    assert resolved == ["Bear", "Bear", "Bull", "Bull"]


def test_resolve_ai_overlay_respects_horizon_end() -> None:
    market_index = pd.to_datetime(
        [
            "2025-01-01T00:00:00+00:00",
            "2025-01-01T01:00:00+00:00",
            "2025-01-01T02:00:00+00:00",
            "2025-01-01T03:00:00+00:00",
        ],
        utc=True,
    )
    overlay = resolve_ai_forecast_overlay_per_bar(
        market_index=market_index,
        ai_rows=[
            {
                "signal_time_utc": "2025-01-01T01:00:00+00:00",
                "horizon_end_utc": "2025-01-01T02:00:00+00:00",
                "predicted_trend": "bull",
                "confidence_bull": 80.0,
                "confidence_bear": 10.0,
                "confidence_flat": 20.0,
            }
        ],
        fallback_regime="Bear",
        bull_threshold=70.0,
        bear_threshold=70.0,
    )

    assert overlay.regimes == ["Bear", "Bull", "Bull", "Bear"]
    assert overlay.active == [False, True, True, False]
    assert overlay.applied == [False, True, True, False]


def test_ai_side_lock_filters_opposite_trade_and_recomputes_summary() -> None:
    df = _frame(20)
    service = BacktestingService()
    result = {
        "summary": {"total_trades": 2},
        "trades": [
            {
                "side": "LONG",
                "entry_time": df.index[4].isoformat(),
                "exit_time": df.index[6].isoformat(),
                "exit_reason": "TAKE",
                "pnl_usdt": 20.0,
                "risk_usdt": 10.0,
            },
            {
                "side": "SHORT",
                "entry_time": df.index[5].isoformat(),
                "exit_time": df.index[7].isoformat(),
                "exit_reason": "STOP",
                "pnl_usdt": -10.0,
                "risk_usdt": 10.0,
            },
        ],
        "chart_points": {},
        "explanations": [],
    }
    payload = {
        "run_with_ai": True,
        "ai_forecast_rows": [
            {
                "signal_time_utc": df.index[0].isoformat(),
                "predicted_trend": "bull",
                "confidence_bull": 90.0,
                "confidence_bear": 5.0,
                "confidence_flat": 5.0,
            }
        ],
        "ai_bull_confidence_threshold": 70.0,
        "ai_bear_confidence_threshold": 70.0,
    }

    filtered = service._apply_ai_side_lock(result, df, payload, 1000.0)

    assert len(filtered["trades"]) == 1
    assert filtered["trades"][0]["side"] == "LONG"
    assert filtered["summary"]["ai_filtered_trades"] == 1
    assert filtered["summary"]["total_pnl"] == 20.0
    assert filtered["explanations"][-1]["type"] == "ai_entry_side_lock"


def test_atr_ob_snapshot_contains_pnl_usdt() -> None:
    df = _frame()
    result = run_atr_order_block(df, {"allocation_usdt": 500.0})
    assert set(result.keys()) == {"summary", "trades", "chart_points", "explanations"}
    _assert_capital_metrics(result)
    if result["trades"]:
        assert "pnl_usdt" in result["trades"][0]
        _assert_trade_confirmation_shape(result["trades"][0])


def test_grid_snapshot_supports_order_size_and_eod_close() -> None:
    df = _frame()
    result = run_grid_bot(
        df,
        {
            "initial_capital_usdt": 1000.0,
            "order_size_usdt": 100.0,
            "close_open_positions_on_eod": True,
        },
    )
    assert set(result.keys()) == {"summary", "trades", "chart_points", "explanations"}
    _assert_capital_metrics(result)
    if result["trades"]:
        assert result["trades"][-1]["exit_reason"] in {"GRID_TP", "EOD_CLOSE"}
        _assert_trade_confirmation_shape(result["trades"][0])


def test_intraday_snapshot_supports_fixed_entry_size() -> None:
    df = _frame()
    result = run_intraday_momentum(
        df,
        {
            "side": "long",
            "entry_size_usdt": 50.0,
            "allocation_usdt": 1000.0,
            "fee_pct": 0.06,
        },
    )
    assert set(result.keys()) == {"summary", "trades", "chart_points", "explanations"}
    _assert_capital_metrics(result)
    if result["trades"]:
        _assert_trade_confirmation_shape(result["trades"][0])
