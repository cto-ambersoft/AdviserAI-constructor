from dataclasses import dataclass

import numpy as np


@dataclass
class PositionSizer:
    account_balance: float
    risk_per_trade: float
    max_open_positions: int
    max_position_pct: float = 100.0
    open_trades: int = 0

    def calculate_position_size(
        self,
        entry: float,
        stop_loss: float,
    ) -> dict[str, float | bool | str]:
        if self.open_trades >= self.max_open_positions:
            return {"allowed": False, "reason": "max_positions", "quantity": 0.0, "risk_usdt": 0.0}
        risk_usdt = self.account_balance * (self.risk_per_trade / 100.0)
        sl_distance = abs(entry - stop_loss)
        if sl_distance <= 0:
            return {
                "allowed": False,
                "reason": "invalid_sl_distance",
                "quantity": 0.0,
                "risk_usdt": 0.0,
            }

        quantity = risk_usdt / sl_distance
        position_value = quantity * entry
        max_position_value = self.account_balance * (self.max_position_pct / 100.0)
        if np.isfinite(max_position_value) and max_position_value > 0 and position_value > max_position_value:
            quantity = max_position_value / entry
            position_value = quantity * entry

        return {
            "allowed": True,
            "reason": "ok",
            "quantity": quantity,
            "position_value": position_value,
            "risk_usdt": quantity * sl_distance,
            "risk_usdt_plan": risk_usdt,
        }


def calculate_performance_metrics(trades: list[dict[str, object]]) -> dict[str, float | int]:
    def _to_float(value: object) -> float:
        if isinstance(value, bool):
            return np.nan
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return np.nan
        return np.nan

    if not trades:
        return {
            "total_trades": 0,
            "closed_trades": 0,
            "win_rate": 0.0,
            "wins": 0,
            "losses": 0,
            "profit_factor": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "avg_r": 0.0,
            "total_r": 0.0,
            "max_drawdown": 0.0,
            "consecutive_losses": 0,
        }

    closed = [trade for trade in trades if trade.get("exit_reason") != "OPEN"]
    r_values = [_to_float(trade.get("r_real", np.nan)) for trade in closed]
    r_values = [value for value in r_values if np.isfinite(value)]
    if not r_values:
        return {
            "total_trades": len(trades),
            "closed_trades": len(closed),
            "win_rate": 0.0,
            "wins": 0,
            "losses": 0,
            "profit_factor": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "avg_r": 0.0,
            "total_r": 0.0,
            "max_drawdown": 0.0,
            "consecutive_losses": 0,
        }

    wins = len([value for value in r_values if value > 0])
    losses = len([value for value in r_values if value < 0])
    win_rate = wins / len(r_values) * 100.0 if r_values else 0.0
    total_wins = sum(value for value in r_values if value > 0)
    total_losses = abs(sum(value for value in r_values if value < 0))
    profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")
    cumulative = np.cumsum(r_values)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = running_max - cumulative
    max_drawdown = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0
    streak = 0
    max_streak = 0
    for value in r_values:
        if value < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "profit_factor": profit_factor,
        "best_trade": max(r_values),
        "worst_trade": min(r_values),
        "avg_r": float(np.mean(r_values)),
        "total_r": float(sum(r_values)),
        "max_drawdown": max_drawdown,
        "consecutive_losses": max_streak,
    }
