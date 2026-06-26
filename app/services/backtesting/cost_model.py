"""Shared trading-cost model for backtest engines (Finding 7.4).

Backtest P&L was overstated because several engines ignored trading costs. This
module nets fees, slippage and (optional) funding off each closed trade's
``pnl_usdt`` *uniformly*, so every engine reports realistic P&L. Because the
summary metrics (equity curve, max DD, returns, R-multiple) are derived from
``pnl_usdt`` in ``common.py``, adjusting ``pnl_usdt`` here flows through to the
whole summary.

Conventions (per side, percent of notional — matching the pre-existing
``order_fee_pct`` / ``fee_pct`` engine params):
- ``fee_pct``      — exchange commission per side (e.g. 0.06 = 0.06%).
- ``slippage_pct`` — execution slippage per side (default 0).
- ``funding_pct_per_bar`` — perpetual funding per held bar (default 0 = off).

A zero cost model is a deliberate no-op: ``pnl_usdt`` is left exactly as the
engine produced it, so turning costs off reproduces the previous numbers.
Partial fills are intentionally NOT modelled (documented out-of-scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CostModel:
    fee_pct: float = 0.0
    slippage_pct: float = 0.0
    funding_pct_per_bar: float = 0.0

    @property
    def is_zero(self) -> bool:
        return (
            self.fee_pct == 0.0
            and self.slippage_pct == 0.0
            and self.funding_pct_per_bar == 0.0
        )


_DEFAULT_FEE_PCT = 0.06


def cost_model_from_params(params: dict[str, Any]) -> CostModel:
    """Build a :class:`CostModel` from an engine ``params`` dict.

    Reads ``fee_pct`` (falls back to the legacy ``order_fee_pct`` Grid name),
    ``slippage_pct`` and ``funding_pct_per_bar``. Defaults the commission to
    0.06% per side — the same value Grid/Intraday already used — so every engine
    applies a realistic round-trip fee unless the caller overrides it.
    """
    fee = params.get("fee_pct")
    if fee is None:
        fee = params.get("order_fee_pct", _DEFAULT_FEE_PCT)
    return CostModel(
        fee_pct=_to_float(fee, _DEFAULT_FEE_PCT),
        slippage_pct=_to_float(params.get("slippage_pct", 0.0)),
        funding_pct_per_bar=_to_float(params.get("funding_pct_per_bar", 0.0)),
    )


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


def _entry_notional(trade: dict[str, Any]) -> float:
    """Position value (USDT) at entry, derived from whatever the engine records."""
    for key in ("entry_notional", "notional_usdt", "position_value", "allocation_usdt"):
        if key in trade:
            return abs(_to_float(trade[key]))
    qty = _to_float(trade.get("qty", trade.get("position_size", 0.0)))
    entry = _to_float(trade.get("entry", trade.get("entry_price", 0.0)))
    return abs(qty * entry)


def _exit_notional(trade: dict[str, Any], entry_notional: float) -> float:
    if "exit_notional" in trade:
        return abs(_to_float(trade["exit_notional"]))
    qty = _to_float(trade.get("qty", trade.get("position_size", 0.0)))
    exit_price = _to_float(trade.get("exit_price", trade.get("exit", 0.0)))
    if qty and exit_price:
        return abs(qty * exit_price)
    # pct-based engines: scale entry notional by the price move if available.
    entry = _to_float(trade.get("entry", trade.get("entry_price", 0.0)))
    if entry and exit_price:
        return entry_notional * (exit_price / entry)
    return entry_notional


def _holding_bars(trade: dict[str, Any]) -> float:
    return max(_to_float(trade.get("bars_held", 0.0)), 0.0)


def trade_cost_usdt(
    *,
    entry_notional: float,
    exit_notional: float,
    holding_bars: float,
    cost: CostModel,
) -> float:
    """Total trading cost (USDT) for one trade: round-trip fee + slippage + funding."""
    per_side_rate = cost.fee_pct + cost.slippage_pct
    total = (entry_notional + exit_notional) * per_side_rate / 100.0
    total += entry_notional * cost.funding_pct_per_bar / 100.0 * max(holding_bars, 0.0)
    return total


def apply_cost_model(
    trades: list[dict[str, Any]], cost: CostModel
) -> list[dict[str, Any]]:
    """Net trading costs off each closed trade's ``pnl_usdt`` (in place).

    Skips open trades and trades with no derivable notional. Records ``cost_usdt``
    for transparency and recomputes ``pnl_pct`` as net return on entry notional.
    A zero cost model returns the trades untouched.
    """
    if cost.is_zero:
        return trades
    for trade in trades:
        if trade.get("exit_reason") == "OPEN":
            continue
        entry_notional = _entry_notional(trade)
        if entry_notional <= 0.0:
            continue
        exit_notional = _exit_notional(trade, entry_notional)
        cost_usdt = trade_cost_usdt(
            entry_notional=entry_notional,
            exit_notional=exit_notional,
            holding_bars=_holding_bars(trade),
            cost=cost,
        )
        gross = _to_float(trade.get("pnl_usdt", 0.0))
        net = gross - cost_usdt
        trade["cost_usdt"] = cost_usdt
        trade["pnl_usdt"] = net
        # Preserve the engine's own pnl_pct basis (engines disagree: some use
        # percent of entry, some a fraction of total capital). Scale by net/gross
        # so the cost is reflected without changing the unit/basis; a zero gross
        # cannot be scaled, so it is left as-is (the cost is still in pnl_usdt).
        if "pnl_pct" in trade and gross != 0.0:
            trade["pnl_pct"] = _to_float(trade.get("pnl_pct")) * (net / gross)
    return trades


def refresh_net_pnl_summary(summary: dict[str, Any], trades: list[dict[str, Any]]) -> None:
    """Recompute the engine's headline ``total_pnl_usdt`` and ``win_rate`` from the
    NET (post-:func:`apply_cost_model`) trades, in place.

    Engines that pre-summarise from a gross trade frame otherwise carry a gross
    win-rate (a marginal winner that fees flip to a loss would still count as a
    win) and a gross pnl total. Call this after ``apply_cost_model`` and before
    ``add_capital_metrics`` so the net values flow through.
    """
    closed = [trade for trade in trades if trade.get("exit_reason") != "OPEN"]
    summary["total_pnl_usdt"] = float(
        sum(_to_float(trade.get("pnl_usdt", 0.0)) for trade in closed)
    )
    if closed:
        wins = sum(1 for trade in closed if _to_float(trade.get("pnl_usdt", 0.0)) > 0)
        summary["win_rate"] = wins / len(closed) * 100.0
