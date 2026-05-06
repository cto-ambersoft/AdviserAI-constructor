from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

CRYPTO_YEAR_DAYS = 365.25

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
    "r_squared": "R-Squared (R Curve)",
    "r_cumulative": "R Cumulative",
    "max_drawdown": "Max Drawdown (R)",
    "max_drawdown_pct": "Max Drawdown (%)",
    "sharpe_proxy": "Sharpe Proxy (R)",
    "annualized_return_pct": "Annualized Return (%)",
    "calmar_ratio": "Calmar Ratio",
    "walk_forward_stability": "Walk-Forward Stability",
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


def calculate_performance_metrics(trades: list[dict[str, object]]) -> dict[str, Any]:
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
            "sharpe_proxy": 0.0,
            "walk_forward_stability": build_walk_forward_stability([]),
            "consecutive_losses": 0,
        }

    closed = [trade for trade in trades if trade.get("exit_reason") != "OPEN"]
    r_values = _collect_valid_r_values(closed)
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
            "sharpe_proxy": 0.0,
            "walk_forward_stability": build_walk_forward_stability([]),
            "consecutive_losses": 0,
        }

    wins = len([value for value in r_values if value > 0])
    losses = len([value for value in r_values if value < 0])
    win_rate = wins / len(r_values) * 100.0 if r_values else 0.0
    total_wins = sum(value for value in r_values if value > 0)
    total_losses = abs(sum(value for value in r_values if value < 0))
    profit_factor = total_wins / total_losses if total_losses > 0 else max(0.0, total_wins)
    cumulative = np.cumsum(r_values)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = running_max - cumulative
    max_drawdown = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0
    sharpe_proxy = calculate_sharpe_proxy(r_values)
    walk_forward_stability = build_walk_forward_stability(r_values)
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
        "sharpe_proxy": sharpe_proxy,
        "walk_forward_stability": walk_forward_stability,
        "consecutive_losses": max_streak,
    }


def calculate_sharpe_proxy(r_values: list[float]) -> float:
    if len(r_values) < 2:
        return 0.0
    std = float(np.std(r_values, ddof=1))
    if not np.isfinite(std) or std <= 0:
        return 0.0
    mean = float(np.mean(r_values))
    return float(mean / std * np.sqrt(len(r_values)))


def build_walk_forward_stability(r_values: list[float], windows: int = 4) -> dict[str, Any]:
    if not r_values:
        return {
            "windows": [],
            "positive_windows": 0,
            "stability_score": 0.0,
        }

    segments = np.array_split(np.array(r_values, dtype=float), min(windows, len(r_values)))
    window_metrics: list[dict[str, float | int]] = []
    positive_windows = 0
    for index, segment in enumerate(segments, start=1):
        values = [float(value) for value in segment.tolist() if np.isfinite(value)]
        total_r = float(sum(values))
        if total_r > 0:
            positive_windows += 1
        wins = len([value for value in values if value > 0])
        window_metrics.append(
            {
                "window": index,
                "trades": len(values),
                "win_rate": float(wins / len(values) * 100.0) if values else 0.0,
                "avg_r": float(np.mean(values)) if values else 0.0,
                "total_r": total_r,
            }
        )

    stability_score = positive_windows / len(window_metrics) if window_metrics else 0.0
    return {
        "windows": window_metrics,
        "positive_windows": positive_windows,
        "stability_score": float(stability_score),
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
    period_start: object | None = None,
    period_end: object | None = None,
) -> tuple[dict[str, Any], list[dict[str, float | int | str | None]]]:
    initial = float(initial_balance)
    closed_pnls: list[float] = []
    risks: list[float] = []
    for trade in trades:
        if trade.get("exit_reason") == "OPEN":
            trade["r_multiple"] = None
            continue
        pnl = _to_float(trade.get("pnl_usdt", np.nan))
        if np.isfinite(pnl):
            closed_pnls.append(float(pnl))
        risk = _to_float(trade.get("risk_usdt", np.nan))
        if np.isfinite(risk) and risk > 0:
            risks.append(float(risk))
        trade["r_multiple"] = compute_trade_r_multiple(trade)

    r_values = _collect_valid_r_values(trades)
    r_cumulative_curve = build_r_cumulative_curve(r_values)
    r_cumulative = float(r_cumulative_curve[-1]) if r_cumulative_curve else 0.0
    avg_r = float(np.mean(r_values)) if r_values else 0.0
    r_squared = calculate_r_squared(r_cumulative_curve, valid_r_count=len(r_values))
    performance_summary = calculate_performance_metrics(trades)

    total_pnl = float(sum(closed_pnls))
    final_balance = float(initial + total_pnl)
    avg_risk = float(np.mean(risks)) if risks else 0.0
    equity_curve = build_equity_curve(trades, initial)
    total_return_pct = calculate_total_return_pct(initial, final_balance)
    max_drawdown_pct = calculate_equity_max_drawdown_pct(equity_curve)
    period_days = calculate_backtest_period_days(
        trades=trades,
        period_start=period_start,
        period_end=period_end,
    )
    annualized_return_pct = calculate_annualized_return_pct(
        initial_balance=initial,
        final_balance=final_balance,
        period_days=period_days,
    )
    calmar_ratio = calculate_calmar_ratio(
        annualized_return_pct=annualized_return_pct,
        max_drawdown_pct=max_drawdown_pct,
    )
    enriched_summary = dict(performance_summary)
    enriched_summary.update(summary)
    enriched_summary.update(
        {
            "initial_balance": initial,
            "final_balance": final_balance,
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "avg_risk_per_trade": avg_risk,
            "avg_r": avg_r,
            "total_r": r_cumulative,
            "r_cumulative": r_cumulative,
            "r_squared": r_squared,
            "max_drawdown_pct": max_drawdown_pct,
            "annualized_return_pct": annualized_return_pct,
            "calmar_ratio": calmar_ratio,
        }
    )
    return add_client_summary_fields(enriched_summary), equity_curve


def calculate_total_return_pct(initial_balance: float, final_balance: float) -> float:
    if not (np.isfinite(initial_balance) and initial_balance > 0 and np.isfinite(final_balance)):
        return 0.0
    return _finite_or_zero((float(final_balance) / float(initial_balance) - 1.0) * 100.0)


def calculate_equity_max_drawdown_pct(
    equity_curve: list[dict[str, float | int | str | None]],
) -> float:
    peak = -np.inf
    max_drawdown = 0.0
    for point in equity_curve:
        equity = _to_float(point.get("equity", np.nan))
        if not np.isfinite(equity):
            continue
        peak = max(peak, float(equity))
        if not np.isfinite(peak) or peak <= 0:
            continue
        max_drawdown = max(max_drawdown, (peak - float(equity)) / peak)
    return _finite_or_zero(max_drawdown * 100.0)


def calculate_backtest_period_days(
    trades: list[dict[str, Any]],
    period_start: object | None = None,
    period_end: object | None = None,
) -> float:
    start_ts = _to_utc_timestamp_seconds(period_start)
    end_ts = _to_utc_timestamp_seconds(period_end)
    if start_ts is None or end_ts is None:
        trade_times = [
            timestamp
            for timestamp in (
                _to_utc_timestamp_seconds(trade.get("exit_time") or trade.get("exitTime"))
                for trade in trades
                if trade.get("exit_reason") != "OPEN"
            )
            if timestamp is not None
        ]
        if start_ts is None and trade_times:
            start_ts = min(trade_times)
        if end_ts is None and trade_times:
            end_ts = max(trade_times)
    if start_ts is None or end_ts is None or end_ts <= start_ts:
        return 0.0
    return _finite_or_zero((end_ts - start_ts) / 86_400.0)


def calculate_annualized_return_pct(
    initial_balance: float,
    final_balance: float,
    period_days: float,
) -> float:
    if not (
        np.isfinite(initial_balance)
        and initial_balance > 0
        and np.isfinite(final_balance)
        and final_balance > 0
        and np.isfinite(period_days)
        and period_days > 0
    ):
        return 0.0
    try:
        annualized = (float(final_balance) / float(initial_balance)) ** (
            CRYPTO_YEAR_DAYS / float(period_days)
        ) - 1.0
    except OverflowError:
        return 0.0
    return _finite_or_zero(annualized * 100.0)


def calculate_calmar_ratio(annualized_return_pct: float, max_drawdown_pct: float) -> float:
    if not (
        np.isfinite(annualized_return_pct)
        and np.isfinite(max_drawdown_pct)
        and max_drawdown_pct > 0
    ):
        return 0.0
    return _finite_or_zero(float(annualized_return_pct) / float(max_drawdown_pct))


def compute_trade_r_multiple(trade: dict[str, Any]) -> float | None:
    r_real = _to_float(trade.get("r_real", np.nan))
    if np.isfinite(r_real):
        return float(r_real)

    pnl_usdt = _to_float(trade.get("pnl_usdt", np.nan))
    if not np.isfinite(pnl_usdt):
        return None

    risk_usdt = _to_float(trade.get("risk_usdt", np.nan))
    if not (np.isfinite(risk_usdt) and risk_usdt > 0):
        risk_usdt = _derive_trade_risk_usdt(trade)
    if not (np.isfinite(risk_usdt) and risk_usdt > 0):
        return None
    return float(pnl_usdt / risk_usdt)


def build_r_cumulative_curve(r_multiples: list[float]) -> list[float]:
    if not r_multiples:
        return [0.0]
    cumulative = np.cumsum(np.asarray(r_multiples, dtype=float), dtype=float)
    return [0.0, *[float(value) for value in cumulative]]


def calculate_r_squared(r_cumulative_curve: list[float], valid_r_count: int) -> float:
    if valid_r_count < 2:
        return 0.0
    y = np.asarray(r_cumulative_curve, dtype=float)
    if y.size < 2:
        return 0.0
    x = np.arange(y.size, dtype=float)
    design = np.column_stack((x, np.ones_like(x)))
    coeffs, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
    fitted = design @ coeffs
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    if not np.isfinite(ss_tot) or ss_tot <= 0:
        return 0.0
    ss_res = float(np.sum((y - fitted) ** 2))
    if not np.isfinite(ss_res):
        return 0.0
    value = 1.0 - (ss_res / ss_tot)
    return float(np.clip(value, 0.0, 1.0))


def build_r_chart_points(trades: list[dict[str, Any]]) -> dict[str, list[float]]:
    r_values = _collect_valid_r_values(trades)
    r_cumulative_curve = build_r_cumulative_curve(r_values)
    return {
        "r_cumulative_curve": r_cumulative_curve,
        "r_equity_curve": list(r_cumulative_curve),
    }


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


def _to_utc_timestamp_seconds(value: object | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _datetime_to_utc_timestamp(value)
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        parsed = to_pydatetime()
        if isinstance(parsed, datetime):
            return _datetime_to_utc_timestamp(parsed)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return _datetime_to_utc_timestamp(parsed)
    return None


def _datetime_to_utc_timestamp(value: datetime) -> float:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    timestamp = normalized.timestamp()
    return float(timestamp) if np.isfinite(timestamp) else 0.0


def _finite_or_zero(value: float) -> float:
    return float(value) if np.isfinite(value) else 0.0


def _collect_valid_r_values(trades: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for trade in trades:
        if trade.get("exit_reason") == "OPEN":
            continue
        r_multiple = _to_float(trade.get("r_multiple", np.nan))
        if not np.isfinite(r_multiple):
            inferred = compute_trade_r_multiple(trade)
            if inferred is None:
                continue
            r_multiple = inferred
        values.append(float(r_multiple))
    return values


def _derive_trade_risk_usdt(trade: dict[str, Any]) -> float:
    entry = _to_float(trade.get("entry", np.nan))
    sl = _to_float(trade.get("sl", np.nan))
    if not (np.isfinite(entry) and np.isfinite(sl)):
        return np.nan
    price_risk = abs(entry - sl)
    if price_risk <= 0:
        return np.nan

    quantity = _to_float(trade.get("position_size", np.nan))
    if not (np.isfinite(quantity) and quantity > 0):
        quantity = _to_float(trade.get("qty", np.nan))
    if np.isfinite(quantity) and quantity > 0:
        return float(price_risk * abs(quantity))

    allocation_usdt = _to_float(trade.get("allocation_usdt", np.nan))
    if np.isfinite(allocation_usdt) and allocation_usdt > 0 and entry > 0:
        return float((price_risk / abs(entry)) * allocation_usdt)

    return np.nan


def _snake_to_camel(value: str) -> str:
    parts = value.split("_")
    if not parts:
        return value
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
