from typing import Any

import numpy as np
import pandas as pd

from app.services.backtesting.common import add_capital_metrics, annotate_trade_confirmations


def grid_bot_backtest(
    df: pd.DataFrame,
    ma_period: int = 50,
    grid_spacing_pct: float = 0.5,
    grids_down: int = 8,
    order_fee_pct: float = 0.06,
    initial_capital_usdt: float = 1000.0,
    order_size_usdt: float | None = None,
    close_open_positions_on_eod: bool = True,
) -> pd.DataFrame:
    work = df.copy()
    work["ma"] = work["close"].rolling(ma_period).mean()
    spacing = grid_spacing_pct / 100.0
    fee = order_fee_pct / 100.0
    per_order_usdt = (
        float(order_size_usdt)
        if order_size_usdt is not None
        else float(initial_capital_usdt) / max(1, grids_down)
    )
    cash = float(initial_capital_usdt)
    positions: dict[int, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    for i in range(ma_period + 2, len(work)):
        row = work.iloc[i]
        center = row["ma"]
        if not np.isfinite(center) or center <= 0:
            continue
        low = float(row["low"])
        high = float(row["high"])
        time = work.index[i]
        for level_idx in range(1, grids_down + 1):
            level_price = float(center * (1 - spacing * level_idx))
            if level_idx not in positions and low <= level_price:
                if cash < per_order_usdt:
                    continue
                qty = per_order_usdt / level_price
                cash -= per_order_usdt * (1 + fee)
                positions[level_idx] = {"entry": level_price, "qty": qty, "entry_time": time}
        to_close: list[int] = []
        for level_idx, pos in positions.items():
            take_price = float(pos["entry"] * (1 + spacing))
            if high >= take_price:
                qty = float(pos["qty"])
                gross = (take_price - float(pos["entry"])) * qty
                fees = (float(pos["entry"]) * qty + take_price * qty) * fee
                pnl = gross - fees
                trades.append(
                    {
                        "strategy": "Grid BOT",
                        "side": "LONG",
                        "entry_time": pos["entry_time"],
                        "exit_time": time,
                        "entry": float(pos["entry"]),
                        "exit": take_price,
                        "qty": qty,
                        "pnl_usdt": pnl,
                        "pnl_pct": pnl / initial_capital_usdt
                        if initial_capital_usdt > 0
                        else np.nan,
                        "exit_reason": "GRID_TP",
                    }
                )
                cash += take_price * qty * (1 - fee)
                to_close.append(level_idx)
        for level_idx in to_close:
            positions.pop(level_idx, None)

    if close_open_positions_on_eod and positions:
        last_row = work.iloc[-1]
        last_time = work.index[-1]
        last_close = float(last_row["close"])
        for _, pos in positions.items():
            qty = float(pos["qty"])
            entry = float(pos["entry"])
            gross = (last_close - entry) * qty
            fees = (entry * qty + last_close * qty) * fee
            pnl = gross - fees
            trades.append(
                {
                    "strategy": "Grid BOT",
                    "side": "LONG",
                    "entry_time": pos["entry_time"],
                    "exit_time": last_time,
                    "entry": entry,
                    "exit": last_close,
                    "qty": qty,
                    "pnl_usdt": pnl,
                    "pnl_pct": pnl / initial_capital_usdt if initial_capital_usdt > 0 else np.nan,
                    "exit_reason": "EOD_CLOSE",
                }
            )
            cash += last_close * qty * (1 - fee)

    return pd.DataFrame(trades)


def run_grid_bot(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    initial_capital_usdt = float(
        params.get("initial_capital_usdt", params.get("allocation_usdt", 1000.0))
    )
    trades_df = grid_bot_backtest(
        df=df,
        ma_period=int(params.get("ma_period", 50)),
        grid_spacing_pct=float(params.get("grid_spacing_pct", 0.5)),
        grids_down=int(params.get("grids_down", 8)),
        order_fee_pct=float(params.get("order_fee_pct", 0.06)),
        initial_capital_usdt=initial_capital_usdt,
        order_size_usdt=(
            float(params["order_size_usdt"]) if params.get("order_size_usdt") is not None else None
        ),
        close_open_positions_on_eod=bool(params.get("close_open_positions_on_eod", True)),
    )
    if trades_df.empty:
        summary = {"total_trades": 0, "win_rate": 0.0, "total_pnl_usdt": 0.0}
    else:
        summary = {
            "total_trades": int(len(trades_df)),
            "win_rate": float((trades_df["pnl_usdt"] > 0).mean() * 100),
            "total_pnl_usdt": float(trades_df["pnl_usdt"].sum()),
        }
    trades = annotate_trade_confirmations(trades_df.to_dict(orient="records"))
    summary, equity_curve = add_capital_metrics(
        summary=summary,
        trades=trades,
        initial_balance=initial_capital_usdt,
    )
    return {
        "summary": summary,
        "trades": trades,
        "chart_points": {
            "ohlcv": df.reset_index().to_dict(orient="records"),
            "equity_curve": equity_curve,
        },
        "explanations": [],
    }
