"""Strategy Health Score (W8 — T2.1).

A composite, on-read health metric over a strategy's *live closed positions*,
computed by reusing the backtest metric library (``backtesting/common.py``) so
live and backtest numbers stay consistent. No table — this is recomputed per
request; persisted snapshots are deferred to W9 (when the KPI-Guard needs
history).

``health_score`` is a 0–100 composite of win rate, drawdown, PnL and walk-
forward stability. The weights and normalization references are **named
constants, calibrated against live outcomes in W9** — the W8 values are
deliberate, conservative placeholders. Crucially, a strategy with too few
closed trades returns ``insufficient_data`` rather than a misleadingly low
``critical`` score, so a fresh strategy is never auto-judged on noise.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.exchange_trade_ledger import ExchangeTradeLedger
from app.models.strategy_health_snapshot import StrategyHealthSnapshot
from app.services.auto_trade.income_sync import sum_funding
from app.services.backtesting.common import (
    build_equity_curve,
    build_walk_forward_stability,
    calculate_equity_max_drawdown_pct,
    calculate_performance_metrics,
    calculate_sharpe_proxy,
    compute_trade_r_multiple,
)
from app.services.execution.futures_pnl import compute_realized_breakdown

_STATUS_CLOSED = "closed"
_SIDE_SHORT = "SHORT"

DEFAULT_WINDOW_DAYS = 30
# Below this many closed trades a score would be noise — report insufficient_data.
HEALTH_MIN_TRADES = 10

# Composite weights — W9-calibrated. Must sum to 1.0.
HEALTH_WEIGHT_WIN_RATE = 0.30
HEALTH_WEIGHT_DRAWDOWN = 0.30
HEALTH_WEIGHT_PNL = 0.20
HEALTH_WEIGHT_STABILITY = 0.20

# Normalization references — W9-calibrated.
# A drawdown at/above this (% of the per-trade capital base) scores 0 on the
# drawdown axis; a return of ±this swings the PnL axis fully.
HEALTH_MAX_DD_REFERENCE_PCT = 50.0
HEALTH_PNL_REFERENCE_PCT = 20.0

# Class thresholds on the 0–100 score.
HEALTH_HEALTHY_MIN = 70.0
HEALTH_WARNING_MIN = 40.0

HEALTH_CLASS_INSUFFICIENT = "insufficient_data"


@dataclass(frozen=True)
class StrategyHealth:
    config_id: int
    window_days: int
    sample_size: int
    win_rate_pct: float
    max_dd_pct: float
    total_pnl_usdt: float
    roi_pct: float
    sharpe_proxy: float
    stability_score: float
    health_score: float
    health_class: str
    computed_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _normalization_base_usdt(config: AutoTradeConfig | None) -> float:
    """Capital base for the equity-curve / PnL-% (ROI) normalization.

    The strategy's per-trade ``position_size_usdt`` — a stable, in-process proxy.
    The real sub-account balance is a heavier exchange fetch deliberately deferred
    to W9 calibration (review C1: no exchange call on this read path). Keeping the
    base in one named place (I6) means every downstream metric that divides by it
    — ``max_dd_pct``, ``roi_pct`` — moves together when the base is recalibrated.
    """
    return float(config.position_size_usdt) if config is not None else 0.0


def _roi_pct(*, total_pnl_usdt: float, base_usdt: float) -> float:
    """Realized return as a percent of the normalization base.

    A raw return ratio (same category as ``total_pnl_usdt``), so it is reported on
    every path — including ``insufficient_data`` — unlike the noisy statistical
    metrics. ``0.0`` when the base is non-positive, so a misconfigured strategy
    never yields ``inf``/``nan`` ROI.
    """
    if base_usdt <= 0:
        return 0.0
    return total_pnl_usdt / base_usdt * 100.0


def gross_realized_pnl(position: AutoTradePosition) -> float | None:
    """Gross realized PnL of a closed position from stored prices — no exchange.

    Same shape as the daily-loss aggregate (review C1): fees are excluded (a W9
    refinement), and we never call ``build_position_pnl_snapshot`` here, which
    would issue a live ``fetch_futures_trades`` per position on the dashboard
    read path. ``None`` when the close price is unknown.
    """
    if position.close_price is None:
        return None
    entry = float(position.entry_price)
    close = float(position.close_price)
    qty = float(position.quantity)
    if position.side == _SIDE_SHORT:
        return (entry - close) * qty
    return (close - entry) * qty


def _position_to_trade(*, realized_pnl: float, position: AutoTradePosition) -> dict[str, Any]:
    """Map a closed position to the trade-dict shape ``common.py`` consumes.

    ``entry`` + ``sl`` + ``position_size`` let ``compute_trade_r_multiple`` derive
    the per-trade risk (``|entry-sl| * qty``) and hence the R-multiple.
    """
    return {
        "pnl_usdt": float(realized_pnl),
        "exit_reason": position.close_reason or _STATUS_CLOSED,
        "exit_time": position.closed_at.isoformat() if position.closed_at is not None else None,
        "entry": float(position.entry_price),
        "sl": float(position.sl_price),
        "position_size": float(position.quantity),
    }


def _composite_score(
    *, win_rate_pct: float, max_dd_pct: float, pnl_pct: float, stability_score: float
) -> float:
    win_rate_axis = _clamp01(win_rate_pct / 100.0)
    drawdown_axis = _clamp01(1.0 - max_dd_pct / HEALTH_MAX_DD_REFERENCE_PCT)
    pnl_axis = _clamp01(0.5 + pnl_pct / (2.0 * HEALTH_PNL_REFERENCE_PCT))
    stability_axis = _clamp01(stability_score)
    score = (
        HEALTH_WEIGHT_WIN_RATE * win_rate_axis
        + HEALTH_WEIGHT_DRAWDOWN * drawdown_axis
        + HEALTH_WEIGHT_PNL * pnl_axis
        + HEALTH_WEIGHT_STABILITY * stability_axis
    ) * 100.0
    return round(score, 2)


def _classify(score: float) -> str:
    if score >= HEALTH_HEALTHY_MIN:
        return "healthy"
    if score >= HEALTH_WARNING_MIN:
        return "warning"
    return "critical"


async def _net_realized_by_position(
    *, session: AsyncSession, positions: Sequence[AutoTradePosition]
) -> dict[int, float]:
    """Map ``position.id`` → **net** realized PnL from its synced ledger fills.

    ``net = Σ realized_pnl − commission + funding`` (the same basis as the
    per-account PnL card), so the health KPIs reconcile with what the exchange
    actually booked. Pure DB, no exchange call. Fills are loaded in a single
    ``IN`` query; only positions that have synced fills appear in the result —
    the caller falls back to the stored-price gross for the rest.
    """
    if not positions:
        return {}
    fills = list(
        (
            await session.scalars(
                select(ExchangeTradeLedger)
                .where(
                    ExchangeTradeLedger.auto_trade_position_id.in_(
                        [position.id for position in positions]
                    )
                )
                .order_by(ExchangeTradeLedger.traded_at)
            )
        ).all()
    )
    fills_by_position: dict[int, list[ExchangeTradeLedger]] = {}
    for fill in fills:
        if fill.auto_trade_position_id is not None:
            fills_by_position.setdefault(fill.auto_trade_position_id, []).append(fill)

    positions_by_id = {position.id: position for position in positions}
    net_by_position: dict[int, float] = {}
    for position_id, position_fills in fills_by_position.items():
        position = positions_by_id[position_id]
        funding = await sum_funding(
            session=session,
            account_id=position.account_id,
            symbol=position.symbol,
            start=position.opened_at,
            end=position.closed_at,
        )
        net_by_position[position_id] = compute_realized_breakdown(
            symbol=position.symbol, trades=position_fills, funding=funding
        ).net_realized
    return net_by_position


async def _account_trades_for_health(
    *, session: AsyncSession, config: AutoTradeConfig | None, cutoff: datetime
) -> list[dict[str, Any]]:
    """Per-closed-trade PnL from ALL the account's synced fills in the window.

    One sub-account per strategy → every fill on the account is the strategy's
    (auto **and** manual). Each closing fill (non-zero exchange ``realized_pnl``)
    is one trade. The authoritative account net (``Σ realized − commission +
    funding`` via ``compute_realized_breakdown``) is distributed across closes by
    traded quantity, so the per-trade nets **sum to the exact account net** (the
    same number as the PnL card) while win-rate / drawdown / sharpe are computed
    on a real per-trade series. Pure DB; ``[]`` when no fills are synced (caller
    then falls back to the stored-position basis).
    """
    if config is None:
        return []
    fills = list(
        (
            await session.scalars(
                select(ExchangeTradeLedger)
                .where(
                    ExchangeTradeLedger.account_id == config.account_id,
                    ExchangeTradeLedger.traded_at >= cutoff,
                )
                .order_by(ExchangeTradeLedger.traded_at)
            )
        ).all()
    )
    if not fills:
        return []
    by_symbol: dict[str, list[ExchangeTradeLedger]] = {}
    for fill in fills:
        by_symbol.setdefault(fill.symbol, []).append(fill)
    account_net = 0.0
    gross_total = 0.0
    for symbol, symbol_fills in by_symbol.items():
        funding = await sum_funding(
            session=session, account_id=config.account_id, symbol=symbol, start=cutoff
        )
        account_net += compute_realized_breakdown(
            symbol=symbol, trades=symbol_fills, funding=funding
        ).net_realized
        gross_total += sum(
            float(f.realized_pnl) for f in symbol_fills if f.realized_pnl is not None
        )
    closes = [
        fill
        for fill in fills
        if fill.realized_pnl is not None and float(fill.realized_pnl) != 0.0
    ]
    if not closes:
        return []
    total_close_qty = sum(float(fill.amount) for fill in closes)
    # Distribute (gross − net) [= fees − funding] across closes by qty, so the
    # per-trade nets sum exactly to the account net.
    adjustment = gross_total - account_net
    trades: list[dict[str, Any]] = []
    for fill in closes:
        share = (
            float(fill.amount) / total_close_qty
            if total_close_qty > 0
            else 1.0 / len(closes)
        )
        net = float(fill.realized_pnl) - adjustment * share
        trades.append(
            {
                "pnl_usdt": net,
                # pnl-based win-rate/sharpe: manual fills carry no SL, so there is
                # no R-multiple — use the net PnL itself as the per-trade unit.
                "r_real": net,
                "exit_reason": _STATUS_CLOSED,
                "exit_time": fill.traded_at.isoformat() if fill.traded_at else None,
            }
        )
    return trades


async def compute_strategy_health(
    *,
    session: AsyncSession,
    config_id: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> StrategyHealth:
    """Composite health over the strategy's trades in the last N days.

    Account-scoped: every fill on the strategy's sub-account counts (auto and
    manual), so the KPIs reconcile with the exchange-accurate account PnL card.
    Falls back to the strategy's stored closed positions when no fills are synced
    yet. Pure DB — no exchange round-trips.
    """
    cutoff = _utc_now() - timedelta(days=window_days)
    config = await session.get(AutoTradeConfig, config_id)
    # Normalization base for the equity curve, max_dd_pct and roi_pct (see helper).
    initial_balance = _normalization_base_usdt(config)

    trades = await trades_for_health(
        session=session, config=config, config_id=config_id, cutoff=cutoff
    )

    return health_from_trades(
        config_id=config_id,
        trades=trades,
        initial_balance=initial_balance,
        window_days=window_days,
    )


async def trades_for_health(
    *,
    session: AsyncSession,
    config: AutoTradeConfig | None,
    config_id: int,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    """Per-trade series for health / portfolio metrics over the window.

    Account ledger fills (auto + manual), falling back to the strategy's stored
    closed positions when no fills are synced yet. Shared by
    ``compute_strategy_health`` and the merged-equity portfolio DD (T12) so both
    judge the same trade source. Pure DB — no exchange round-trips.
    """
    trades = await _account_trades_for_health(session=session, config=config, cutoff=cutoff)
    if trades:
        return trades
    # Fallback: stored-position basis (no synced fills yet). Ledger net per position
    # when available, else the stored-price gross.
    positions = (
        await session.scalars(
            select(AutoTradePosition)
            .where(
                AutoTradePosition.config_id == config_id,
                AutoTradePosition.status == _STATUS_CLOSED,
                AutoTradePosition.closed_at >= cutoff,
            )
            .order_by(AutoTradePosition.closed_at.asc())
        )
    ).all()
    net_by_position = await _net_realized_by_position(session=session, positions=positions)
    for position in positions:
        realized = net_by_position.get(position.id)
        if realized is None:
            realized = gross_realized_pnl(position)
        if realized is None or not math.isfinite(realized):
            continue
        trades.append(_position_to_trade(realized_pnl=realized, position=position))
    return trades


def health_from_trades(
    *,
    config_id: int,
    trades: list[dict[str, Any]],
    initial_balance: float,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> StrategyHealth:
    """Pure ``trades`` → :class:`StrategyHealth`.

    Shared by live health (from closed positions) and the promotion-pipeline
    **sandbox validation** (from backtest trades) so live and sandbox KPIs are
    computed by the exact same metric logic. ``trades`` are dicts carrying a
    ``pnl_usdt`` key (the position/backtest trade shape); ``initial_balance`` is
    the equity-curve / drawdown / ROI normalization base. Fail-safe: fewer than
    ``HEALTH_MIN_TRADES`` returns ``insufficient_data`` (never judged on noise).
    """
    sample_size = len(trades)
    total_pnl = float(sum(float(trade["pnl_usdt"]) for trade in trades))
    computed_at = _utc_now()

    if sample_size < HEALTH_MIN_TRADES:
        return StrategyHealth(
            config_id=config_id,
            window_days=window_days,
            sample_size=sample_size,
            win_rate_pct=0.0,
            max_dd_pct=0.0,
            total_pnl_usdt=total_pnl,
            roi_pct=_roi_pct(total_pnl_usdt=total_pnl, base_usdt=initial_balance),
            sharpe_proxy=0.0,
            stability_score=0.0,
            health_score=0.0,
            health_class=HEALTH_CLASS_INSUFFICIENT,
            computed_at=computed_at,
        )

    performance = calculate_performance_metrics(trades)
    r_values = [r for trade in trades if (r := compute_trade_r_multiple(trade)) is not None]
    win_rate_pct = float(performance["win_rate"])
    sharpe_proxy = calculate_sharpe_proxy(r_values)
    stability_score = float(build_walk_forward_stability(r_values)["stability_score"])
    max_dd_pct = calculate_equity_max_drawdown_pct(build_equity_curve(trades, initial_balance))
    roi_pct = _roi_pct(total_pnl_usdt=total_pnl, base_usdt=initial_balance)

    health_score = _composite_score(
        win_rate_pct=win_rate_pct,
        max_dd_pct=max_dd_pct,
        pnl_pct=roi_pct,
        stability_score=stability_score,
    )
    return StrategyHealth(
        config_id=config_id,
        window_days=window_days,
        sample_size=sample_size,
        win_rate_pct=win_rate_pct,
        max_dd_pct=max_dd_pct,
        total_pnl_usdt=total_pnl,
        roi_pct=roi_pct,
        sharpe_proxy=sharpe_proxy,
        stability_score=stability_score,
        health_score=health_score,  # rounded inside _composite_score; range-only contract
        health_class=_classify(health_score),
        computed_at=computed_at,
    )


async def record_health_snapshot(
    *, session: AsyncSession, health: StrategyHealth, user_id: int
) -> StrategyHealthSnapshot:
    """Append one health reading to the ``strategy_health_snapshots`` time series.

    The table is append-only (no unique key), so the KPI-Guard cron and the
    on-close fast path can both write without ever colliding on a constraint
    (W8 I7 does not apply here). ``user_id`` is passed in by the caller — which
    already holds the config — rather than re-queried. The caller owns the
    transaction; this only ``flush``es so the row gets its id and is visible
    within the same session (mirrors the ``commit=False`` event convention).
    """
    row = StrategyHealthSnapshot(
        config_id=health.config_id,
        user_id=user_id,
        window_days=health.window_days,
        sample_size=health.sample_size,
        win_rate_pct=health.win_rate_pct,
        max_dd_pct=health.max_dd_pct,
        total_pnl_usdt=health.total_pnl_usdt,
        roi_pct=health.roi_pct,
        sharpe_proxy=health.sharpe_proxy,
        stability_score=health.stability_score,
        health_score=health.health_score,
        health_class=health.health_class,
        computed_at=health.computed_at,
        payload={},
    )
    session.add(row)
    await session.flush()
    return row


async def get_latest_health_snapshot(
    *, session: AsyncSession, config_id: int
) -> StrategyHealthSnapshot | None:
    """Most recent persisted snapshot for a strategy — the KPI-Guard read path.

    Served by the ``(config_id, computed_at)`` composite index; ``id`` breaks ties
    when two snapshots share a ``computed_at``.
    """
    return (
        await session.scalars(
            select(StrategyHealthSnapshot)
            .where(StrategyHealthSnapshot.config_id == config_id)
            .order_by(
                StrategyHealthSnapshot.computed_at.desc(),
                StrategyHealthSnapshot.id.desc(),
            )
            .limit(1)
        )
    ).first()


async def latest_health_snapshots_for_configs(
    *, session: AsyncSession, config_ids: Sequence[int]
) -> dict[int, StrategyHealthSnapshot]:
    """Latest snapshot per config for a set of configs, in **one** query (review S2).

    The portfolio view needs the most-recent snapshot for every strategy; doing it
    per-config was an N-query loop. The table is append-only with an autoincrement
    ``id``, so ``max(id)`` per ``config_id`` is the latest row (matches
    ``get_latest_health_snapshot``'s ordering when ``computed_at`` is monotonic).
    """
    if not config_ids:
        return {}
    latest_ids = (
        select(func.max(StrategyHealthSnapshot.id))
        .where(StrategyHealthSnapshot.config_id.in_(config_ids))
        .group_by(StrategyHealthSnapshot.config_id)
    ).scalar_subquery()
    rows = (
        await session.scalars(
            select(StrategyHealthSnapshot).where(StrategyHealthSnapshot.id.in_(latest_ids))
        )
    ).all()
    return {row.config_id: row for row in rows}


async def prune_strategy_health_snapshots(
    *, session: AsyncSession, config_id: int, cutoff: datetime
) -> int:
    """Delete a config's snapshots older than ``cutoff`` (retention — review S1).

    Append-only at one row per config per cron tick, the table would grow without
    bound; the W9 guard sweep calls this per config each run. Scoped delete uses
    the ``(config_id, computed_at)`` composite index. Caller controls the commit.
    Returns the number of rows deleted.
    """
    result = await session.execute(
        delete(StrategyHealthSnapshot).where(
            StrategyHealthSnapshot.config_id == config_id,
            StrategyHealthSnapshot.computed_at < cutoff,
        )
    )
    # session.execute() is typed Result; DML returns a CursorResult with rowcount.
    return int(cast(Any, result).rowcount or 0)
