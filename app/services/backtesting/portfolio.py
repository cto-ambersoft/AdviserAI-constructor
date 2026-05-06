from typing import TYPE_CHECKING, Any

import pandas as pd

from app.schemas.market import MARKET_EXCHANGE_DEFAULT
from app.services.backtesting.common import (
    add_capital_metrics,
    add_client_summary_fields,
    annotate_trade_confirmations,
    build_r_chart_points,
)
from app.services.market_data.service import MarketDataService

if TYPE_CHECKING:
    from app.services.backtesting.service import BacktestingService

DISPLAY_TO_ENGINE = {
    "VWAP Builder": "builder_vwap",
    "ATR Order-Block": "atr_order_block",
    "Knife Catcher": "knife_catcher",
    "Grid BOT": "grid_bot",
    "Intraday Momentum": "intraday_momentum",
}
PORTFOLIO_TRADE_COLUMNS = (
    "exit_time",
    "strategy",
    "pnl_usdt",
    "entry_time",
    "side",
    "regime",
    "ai_forecast_applied",
    "ai_base_regime",
    "ai_regime",
    "ai_regime_changed",
    "ai_signal_time_utc",
    "ai_horizon_end_utc",
)


def _normalized_weights(items: list[dict[str, Any]]) -> dict[str, float]:
    weight_map: dict[str, float] = {}
    total = 0.0
    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        weight = float(item.get("weight", 0.0))
        if weight <= 0:
            continue
        weight_map[name] = weight
        total += weight
    if total <= 0:
        names = [
            str(item.get("name", "")).strip() for item in items if str(item.get("name", "")).strip()
        ]
        if not names:
            return {}
        equal = 1.0 / float(len(names))
        return {name: equal for name in names}
    return {name: value / total for name, value in weight_map.items()}


def _resolve_engine(strategy: dict[str, Any]) -> str | None:
    name = str(strategy.get("name", "")).strip()
    config = strategy.get("config", {}) or {}
    strategy_type = str(config.get("strategy_type", "")).strip()
    if strategy_type:
        return strategy_type
    if name in DISPLAY_TO_ENGINE:
        return DISPLAY_TO_ENGINE[name]
    return None


async def _run_strategy_backtest(
    strategy: dict[str, Any],
    backtesting_service: "BacktestingService",
) -> list[dict[str, Any]]:
    engine = _resolve_engine(strategy)
    if engine not in {
        "builder_vwap",
        "atr_order_block",
        "knife_catcher",
        "grid_bot",
        "intraday_momentum",
    }:
        raw_trades = strategy.get("trades", [])
        return raw_trades if isinstance(raw_trades, list) else []

    config = {
        "exchange_name": MARKET_EXCHANGE_DEFAULT,
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars": 500,
        **(strategy.get("config", {}) or {}),
    }

    if engine == "builder_vwap":
        if bool(config.get("run_with_ai", False)):
            comparison = await backtesting_service.run_vwap_with_ai(config)
            ai_result = comparison.get("ai_forecast")
            if not isinstance(ai_result, dict):
                raise ValueError("VWAP AI comparison payload is invalid.")
            ai_trades = ai_result.get("trades", [])
            return ai_trades if isinstance(ai_trades, list) else []
        result = await backtesting_service.run_vwap(config)
    elif engine == "atr_order_block":
        result = await backtesting_service.run_atr_order_block(config)
    elif engine == "knife_catcher":
        result = await backtesting_service.run_knife(config)
    elif engine == "grid_bot":
        result = await backtesting_service.run_grid(config)
    elif engine == "intraday_momentum":
        result = await backtesting_service.run_intraday(config)
    else:
        raw_trades = strategy.get("trades", [])
        return raw_trades if isinstance(raw_trades, list) else []
    resolved_trades = result.get("trades", [])
    return resolved_trades if isinstance(resolved_trades, list) else []


async def run_portfolio(
    strategies: list[dict[str, Any]],
    total_capital: float,
    market_data: MarketDataService | None = None,
) -> dict[str, Any]:
    if not strategies or total_capital <= 0:
        initial = float(total_capital) if total_capital > 0 else 0.0
        return {
            "summary": add_client_summary_fields(
                {
                    "total_strategies": 0,
                    "total_events": 0,
                    "allocated_capital": initial,
                    "final_equity": initial,
                    "initial_balance": initial,
                    "final_balance": initial,
                    "total_pnl_usdt": 0.0,
                    "total_pnl": 0.0,
                    "total_return_pct": 0.0,
                    "avg_risk_per_trade": 0.0,
                    "avg_r": 0.0,
                    "total_r": 0.0,
                    "r_cumulative": 0.0,
                    "r_squared": 0.0,
                    "max_drawdown_pct": 0.0,
                    "annualized_return_pct": 0.0,
                    "calmar_ratio": 0.0,
                }
            ),
            "trades": [],
            "chart_points": {
                "equity": [initial],
                "equity_curve": [{"step": 0, "time": None, "equity": initial, "pnl_usdt": 0.0}],
                "r_cumulative_curve": [0.0],
                "r_equity_curve": [0.0],
            },
            "explanations": [],
        }

    weights = _normalized_weights(strategies)
    strategy_results: list[dict[str, Any]] = []
    market_service = market_data or MarketDataService()
    from app.services.backtesting.service import BacktestingService

    backtesting_service = BacktestingService(market_data=market_service)
    for strategy in strategies:
        strategy_name = str(strategy.get("name", "unknown"))
        engine = _resolve_engine(strategy)
        if engine:
            trades = await _run_strategy_backtest(strategy, backtesting_service)
            strategy_results.append({"name": strategy_name, "trades": trades})
            continue
        strategy_results.append(strategy)

    frames: list[pd.DataFrame] = []
    stats: list[dict[str, Any]] = []
    for strategy in strategy_results:
        strategy_trades_df = pd.DataFrame(strategy.get("trades", []))
        if strategy_trades_df.empty or "pnl_usdt" not in strategy_trades_df.columns:
            continue
        strategy_name = str(strategy.get("name", "unknown"))
        weight = float(weights.get(strategy_name, 0.0))
        if weight <= 0:
            continue
        capital = float(total_capital) * weight
        strategy_trades_df = strategy_trades_df.copy()
        strategy_trades_df["pnl_usdt"] = strategy_trades_df["pnl_usdt"].astype(float)
        if "allocation_usdt" in strategy_trades_df.columns:
            allocated = pd.to_numeric(strategy_trades_df["allocation_usdt"], errors="coerce")
            scale = allocated.replace(0, pd.NA)
            strategy_trades_df["pnl_usdt"] = (
                strategy_trades_df["pnl_usdt"] / scale
            ).fillna(strategy_trades_df["pnl_usdt"]) * capital
        elif "final_pnl" in strategy_trades_df.columns:
            strategy_trades_df["pnl_usdt"] = (
                pd.to_numeric(
                    strategy_trades_df["final_pnl"],
                    errors="coerce",
                ).fillna(0.0)
                * capital
            )
        strategy_trades_df["strategy"] = strategy_name
        frame_columns = [
            column for column in PORTFOLIO_TRADE_COLUMNS if column in strategy_trades_df.columns
        ]
        frames.append(strategy_trades_df[frame_columns])
        stats.append(
            add_client_summary_fields(
                {
                    "strategy": strategy_name,
                    "weight": weight,
                    "allocation_pct": weight * 100.0,
                    "capital": capital,
                    "trades": int(len(strategy_trades_df)),
                    "win_rate": float((strategy_trades_df["pnl_usdt"] > 0).mean() * 100),
                    "total_pnl_usdt": float(strategy_trades_df["pnl_usdt"].sum()),
                }
            )
        )
    if not frames:
        initial = float(total_capital)
        return {
            "summary": add_client_summary_fields(
                {
                    "total_strategies": 0,
                    "total_events": 0,
                    "allocated_capital": initial,
                    "final_equity": initial,
                    "initial_balance": initial,
                    "final_balance": initial,
                    "total_pnl_usdt": 0.0,
                    "total_pnl": 0.0,
                    "total_return_pct": 0.0,
                    "avg_risk_per_trade": 0.0,
                    "avg_r": 0.0,
                    "total_r": 0.0,
                    "r_cumulative": 0.0,
                    "r_squared": 0.0,
                    "max_drawdown_pct": 0.0,
                    "annualized_return_pct": 0.0,
                    "calmar_ratio": 0.0,
                }
            ),
            "trades": [],
            "chart_points": {
                "equity": [initial],
                "equity_curve": [{"step": 0, "time": None, "equity": initial, "pnl_usdt": 0.0}],
                "r_cumulative_curve": [0.0],
                "r_equity_curve": [0.0],
            },
            "explanations": [],
        }
    events = pd.concat(frames, ignore_index=True)
    events["exit_time"] = pd.to_datetime(events["exit_time"])
    events = events.sort_values("exit_time")
    event_rows = events.to_dict(orient="records")
    trades = annotate_trade_confirmations(
        [{str(key): value for key, value in row.items()} for row in event_rows]
    )
    ai_events = len([trade for trade in trades if trade.get("ai_forecast_applied") is True])
    summary, equity_curve = add_capital_metrics(
        summary={
            "total_strategies": len(stats),
            "total_events": int(len(events)),
            "allocated_capital": float(total_capital),
            "final_equity": float(total_capital + events["pnl_usdt"].sum()),
            "total_pnl_usdt": float(events["pnl_usdt"].sum()),
            "ai_forecast_applied": ai_events > 0,
            "ai_forecast_events": ai_events,
        },
        trades=trades,
        initial_balance=float(total_capital),
    )
    r_chart_points = build_r_chart_points(trades)
    equity: list[float] = []
    for point in equity_curve:
        raw_equity = point.get("equity")
        if isinstance(raw_equity, bool):
            equity.append(0.0)
        elif isinstance(raw_equity, (int, float)):
            equity.append(float(raw_equity))
        elif isinstance(raw_equity, str):
            try:
                equity.append(float(raw_equity))
            except ValueError:
                equity.append(0.0)
        else:
            equity.append(0.0)
    return {
        "summary": summary,
        "trades": trades,
        "chart_points": {"equity": equity, "equity_curve": equity_curve, **r_chart_points},
        "explanations": stats,
    }
