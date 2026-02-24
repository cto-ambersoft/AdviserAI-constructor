from typing import Any

import pandas as pd

from app.services.backtesting.atr_order_block import run_atr_order_block
from app.services.backtesting.grid_bot import run_grid_bot
from app.services.backtesting.intraday_momentum import run_intraday_momentum
from app.services.backtesting.knife_catcher import run_knife_catcher
from app.services.backtesting.vwap_builder import run_vwap_backtest
from app.services.indicators.engine import calc_indicators
from app.services.market_data.service import MarketDataService


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
        names = [str(item.get("name", "")).strip() for item in items if str(item.get("name", "")).strip()]
        if not names:
            return {}
        equal = 1.0 / float(len(names))
        return {name: equal for name in names}
    return {name: value / total for name, value in weight_map.items()}


async def run_portfolio(
    strategies: list[dict[str, Any]],
    total_capital: float,
    market_data: MarketDataService | None = None,
) -> dict[str, Any]:
    if not strategies or total_capital <= 0:
        return {
            "summary": {"total_strategies": 0},
            "trades": [],
            "chart_points": {},
            "explanations": [],
        }

    weights = _normalized_weights(strategies)
    strategy_results: list[dict[str, Any]] = []
    market_service = market_data or MarketDataService()
    builtins = {"VWAP Builder", "ATR Order-Block", "Knife Catcher", "Grid BOT", "Intraday Momentum"}
    for strategy in strategies:
        strategy_name = str(strategy.get("name", "unknown"))
        if strategy_name in builtins:
            config = strategy.get("config", {}) or {}
            symbol = str(config.get("symbol", "BTC/USDT"))
            timeframe = str(config.get("timeframe", "1h"))
            bars = int(config.get("bars", 500))
            candles = config.get("candles")
            if candles:
                df = market_service.frame_from_candles(candles)
            else:
                df = await market_service.fetch_ohlcv(
                    exchange_name="bybit",
                    symbol=symbol,
                    timeframe=timeframe,
                    bars=bars,
                )
            if strategy_name == "VWAP Builder":
                result = run_vwap_backtest(df, calc_indicators(df), config)
            elif strategy_name == "ATR Order-Block":
                result = run_atr_order_block(df, config)
            elif strategy_name == "Knife Catcher":
                result = run_knife_catcher(df, config)
            elif strategy_name == "Grid BOT":
                result = run_grid_bot(df, config)
            else:
                result = run_intraday_momentum(df, config)
            strategy_results.append({"name": strategy_name, "trades": result.get("trades", [])})
        else:
            strategy_results.append(strategy)

    frames: list[pd.DataFrame] = []
    stats: list[dict[str, Any]] = []
    for strategy in strategy_results:
        trades = pd.DataFrame(strategy.get("trades", []))
        if trades.empty or "pnl_usdt" not in trades.columns:
            continue
        strategy_name = str(strategy.get("name", "unknown"))
        weight = float(weights.get(strategy_name, 0.0))
        if weight <= 0:
            continue
        capital = float(total_capital) * weight
        trades = trades.copy()
        trades["pnl_usdt"] = trades["pnl_usdt"].astype(float)
        if "allocation_usdt" in trades.columns:
            allocated = pd.to_numeric(trades["allocation_usdt"], errors="coerce")
            scale = allocated.replace(0, pd.NA)
            trades["pnl_usdt"] = (trades["pnl_usdt"] / scale).fillna(trades["pnl_usdt"]) * capital
        elif "final_pnl" in trades.columns:
            trades["pnl_usdt"] = pd.to_numeric(trades["final_pnl"], errors="coerce").fillna(0.0) * capital
        trades["strategy"] = strategy_name
        frames.append(trades[["exit_time", "strategy", "pnl_usdt"]])
        stats.append(
            {
                "strategy": strategy_name,
                "weight": weight,
                "capital": capital,
                "trades": int(len(trades)),
                "win_rate": float((trades["pnl_usdt"] > 0).mean() * 100),
                "total_pnl_usdt": float(trades["pnl_usdt"].sum()),
            }
        )
    if not frames:
        return {
            "summary": {"total_strategies": 0},
            "trades": [],
            "chart_points": {},
            "explanations": [],
        }
    events = pd.concat(frames, ignore_index=True)
    events["exit_time"] = pd.to_datetime(events["exit_time"])
    events = events.sort_values("exit_time")
    equity = [total_capital]
    for pnl in events["pnl_usdt"].values:
        equity.append(equity[-1] + float(pnl))
    summary = {
        "total_strategies": len(stats),
        "total_events": int(len(events)),
        "final_equity": float(equity[-1]),
        "total_pnl_usdt": float(equity[-1] - total_capital),
    }
    return {
        "summary": summary,
        "trades": events.to_dict(orient="records"),
        "chart_points": {"equity": equity},
        "explanations": stats,
    }
