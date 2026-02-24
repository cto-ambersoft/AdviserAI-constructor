from datetime import UTC, datetime, timedelta

from app.services.backtesting.atr_order_block import run_atr_order_block
from app.services.backtesting.grid_bot import run_grid_bot
from app.services.backtesting.intraday_momentum import run_intraday_momentum
from app.services.backtesting.vwap_builder import run_vwap_backtest
from app.services.indicators.engine import calc_indicators
from app.services.market_data.service import MarketDataService


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
    if result["trades"]:
        assert "sl_explain" in result["trades"][0]
    if result["explanations"]:
        assert "sl_explain" in result["explanations"][0]


def test_atr_ob_snapshot_contains_pnl_usdt() -> None:
    df = _frame()
    result = run_atr_order_block(df, {"allocation_usdt": 500.0})
    assert set(result.keys()) == {"summary", "trades", "chart_points", "explanations"}
    if result["trades"]:
        assert "pnl_usdt" in result["trades"][0]


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
    if result["trades"]:
        assert result["trades"][-1]["exit_reason"] in {"GRID_TP", "EOD_CLOSE"}


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
