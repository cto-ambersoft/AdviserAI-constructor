from dataclasses import dataclass
from typing import Any

import numpy as np

SUMMARY_FIELD_LABELS: dict[str, str] = {
    "total_trades": "Total Trades",
    "closed_trades": "Closed Trades",
    "win_rate": "Win Rate (%)",
    "wins": "Winning Trades",
    "losses": "Losing Trades",
    "profit_factor": "Profit Factor",
    "best_trade": "Best Trade (R)",
    "worst_trade": "Worst Trade (R)",
    "avg_r": "Average R",
    "total_r": "Total R",
    "max_drawdown": "Max Drawdown (R)",
    "consecutive_losses": "Max Consecutive Losses",
    "initial_balance": "Initial Balance (USDT)",
    "final_balance": "Final Balance (USDT)",
    "total_pnl": "Total PnL (USDT)",
    "total_pnl_usdt": "Total PnL (USDT)",
    "avg_risk_per_trade": "Average Risk per Trade (USDT)",
    "total_return_pct": "Total Return (%)",
    "final_equity": "Final Equity (USDT)",
    "total_strategies": "Total Strategies",
    "total_events": "Total Events",
}


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
        if (
            np.isfinite(max_position_value)
            and max_position_value > 0
            and position_value > max_position_value
        ):
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


def build_equity_curve(
    trades: list[dict[str, Any]],
    initial_balance: float,
) -> list[dict[str, float | int | str | None]]:
    curve: list[dict[str, float | int | str | None]] = [
        {"step": 0, "time": None, "equity": float(initial_balance), "pnl_usdt": 0.0}
    ]
    equity = float(initial_balance)
    step = 1
    for trade in trades:
        if trade.get("exit_reason") == "OPEN":
            continue
        pnl = _to_float(trade.get("pnl_usdt", np.nan))
        if not np.isfinite(pnl):
            continue
        equity += float(pnl)
        curve.append(
            {
                "step": step,
                "time": str(trade.get("exit_time")) if trade.get("exit_time") is not None else None,
                "equity": float(equity),
                "pnl_usdt": float(pnl),
            }
        )
        step += 1
    return curve


def add_capital_metrics(
    summary: dict[str, Any],
    trades: list[dict[str, Any]],
    initial_balance: float,
) -> tuple[dict[str, Any], list[dict[str, float | int | str | None]]]:
    initial = float(initial_balance)
    closed_pnls: list[float] = []
    risks: list[float] = []
    for trade in trades:
        if trade.get("exit_reason") == "OPEN":
            continue
        pnl = _to_float(trade.get("pnl_usdt", np.nan))
        if np.isfinite(pnl):
            closed_pnls.append(float(pnl))
        risk = _to_float(trade.get("risk_usdt", np.nan))
        if np.isfinite(risk):
            risks.append(float(risk))
    total_pnl = float(sum(closed_pnls))
    final_balance = float(initial + total_pnl)
    avg_risk = float(np.mean(risks)) if risks else 0.0
    enriched_summary = dict(summary)
    enriched_summary.update(
        {
            "initial_balance": initial,
            "final_balance": final_balance,
            "total_pnl": total_pnl,
            "avg_risk_per_trade": avg_risk,
        }
    )
    return add_client_summary_fields(enriched_summary), build_equity_curve(trades, initial)


def add_client_summary_fields(summary: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(summary)
    client_values: dict[str, Any] = {}
    client_labels: dict[str, str] = {}
    client_stats: list[dict[str, Any]] = []
    for key, value in summary.items():
        if key in {"client_values", "client_labels", "client_stats"}:
            continue
        camel_key = _snake_to_camel(key)
        label = SUMMARY_FIELD_LABELS.get(key, key.replace("_", " ").title())
        client_values[camel_key] = value
        client_labels[camel_key] = label
        client_stats.append(
            {
                "source_key": key,
                "key": camel_key,
                "label": label,
                "value": value,
            }
        )
    enriched["client_values"] = client_values
    enriched["client_labels"] = client_labels
    enriched["client_stats"] = client_stats
    return enriched


def annotate_trade_confirmations(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for trade in trades:
        row = dict(trade)
        exit_raw = str(row.get("exit_reason") or row.get("exit_type") or "").upper()
        pnl_usdt = _to_float(row.get("pnl_usdt", np.nan))
        pnl_pct = _to_float(row.get("pnl_pct", np.nan))
        pnl = pnl_usdt if np.isfinite(pnl_usdt) else pnl_pct

        normalized = "OTHER"
        status = "unknown"
        is_tp = False
        is_sl = False
        is_closed = True

        if exit_raw == "OPEN":
            normalized = "OPEN"
            status = "open"
            is_closed = False
        elif exit_raw in {"TAKE", "TP", "GRID_TP"}:
            normalized = "TAKE_PROFIT"
            status = "take_profit"
            is_tp = True
        elif exit_raw in {"STOP", "SL"}:
            normalized = "STOP_LOSS"
            status = "stop_loss"
            is_sl = True
        elif exit_raw == "TIME":
            normalized = "TIME_EXIT"
        elif exit_raw == "EOD_CLOSE":
            normalized = "EOD_CLOSE"

        if status not in {"take_profit", "stop_loss", "open"}:
            if np.isfinite(pnl):
                if pnl > 0:
                    status = "profit"
                elif pnl < 0:
                    status = "loss"
                else:
                    status = "breakeven"

        row["exit_reason_raw"] = exit_raw or None
        row["exit_reason_normalized"] = normalized
        row["confirmation_status"] = status
        row["is_take_profit"] = is_tp
        row["is_stop_loss"] = is_sl
        row["is_closed"] = is_closed
        row["is_profit"] = bool(status in {"take_profit", "profit"})
        # Client-facing aliases for table columns (non-breaking; original keys stay).
        row["outcome"] = status
        row["entryIndex"] = row.get("entry_i")
        row["exitIndex"] = row.get("exit_i")
        row["entryTime"] = row.get("entry_time")
        row["exitTime"] = row.get("exit_time")
        row["entryPrice"] = row.get("entry")
        annotated.append(row)
    return annotated


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


def _snake_to_camel(value: str) -> str:
    parts = value.split("_")
    if not parts:
        return value
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
