"""Aggregated portfolio view across a user's strategies.

W7 — Multi-Strategy Account Partitioning.

A "strategy" is a single :class:`AutoTradeConfig` row, which by design owns
one :class:`ExchangeCredential` (its own physical sub-account on the
exchange). The portfolio view sums PnL across all of a user's strategies
and pulls the live sub-account balance from each exchange in parallel so
the dashboard can show a budget bar per strategy.

This module is intentionally thin glue — heavy lifting (PnL math, position
queries, balance fetches) is delegated to existing services. It exists as a
separate module rather than another method on :class:`AutoTradeService` to
keep the orchestration logic (which calls 3 distinct services) easy to
read and test.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.timeutils import as_aware_utc
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.strategy_health_snapshot import StrategyHealthSnapshot
from app.services.auto_trade.health import (
    _normalization_base_usdt,
    compute_strategy_health,
    latest_health_snapshots_for_configs,
    trades_for_health,
)
from app.services.auto_trade.service import POSITION_OPEN, AutoTradeService
from app.services.backtesting.common import (
    build_equity_curve,
    calculate_equity_max_drawdown_pct,
)
from app.services.execution.trading_service import TradingService


@dataclass(frozen=True)
class StrategyPortfolioEntry:
    config_id: int
    account_id: int
    account_label: str
    exchange_name: str
    mode: str
    # B5 (W10): promotion lifecycle stage — drives the monitor stage badge.
    lifecycle_stage: str
    strategy_name: str | None
    profile_id: int
    profile_symbol: str | None
    is_running: bool
    enabled: bool
    open_positions_count: int
    margin_used_usdt: float
    realized_pnl_usdt: float
    unrealized_pnl_usdt: float
    balance_total_usdt: float | None
    balance_free_usdt: float | None
    last_started_at: datetime | None
    last_stopped_at: datetime | None
    balance_error: str | None
    # W9 T3.2 / B4 — live KPIs for the AC#7 dashboard. Read from the latest
    # strategy_health_snapshot when fresh; a running strategy whose snapshot is
    # missing/stale is recomputed in-process (no exchange round-trip). ``None`` only
    # for a stopped strategy with no snapshot. ``kpi_as_of`` carries the as-of time.
    win_rate_pct: float | None
    max_dd_pct: float | None
    sharpe_proxy: float | None
    roi_pct: float | None
    health_class: str | None
    sample_size: int | None
    # B4 — when the surfaced KPIs were computed. The snapshot's ``computed_at`` when
    # read from the cron-written snapshot, or the live recompute time when the
    # snapshot was missing/stale for a running strategy. ``None`` when no KPIs.
    kpi_as_of: datetime | None


@dataclass(frozen=True)
class PortfolioSummary:
    strategies: list[StrategyPortfolioEntry]
    total_realized_pnl_usdt: float
    total_unrealized_pnl_usdt: float
    total_open_positions: int
    total_running_strategies: int
    # True merged-equity portfolio drawdown (T12/W11a): max drawdown of ONE equity
    # curve over every strategy's closed trades, not the worst single strategy.
    portfolio_max_dd_pct: float


async def _fetch_usdt_balance(
    *,
    trading: TradingService,
    session: AsyncSession,
    user_id: int,
    account_id: int,
) -> tuple[float | None, float | None, str | None]:
    """Fetch (free, total) USDT balance for one exchange sub-account.

    Returns ``(None, None, error_message)`` on failure so a single misbehaving
    exchange does not 500 the whole portfolio endpoint.
    """

    try:
        snapshot = await trading.get_spot_balances(
            session=session, user_id=user_id, account_id=account_id
        )
    except Exception as exc:  # noqa: BLE001 — sub-account failures must not poison portfolio
        return None, None, type(exc).__name__
    free_total = 0.0
    total_total = 0.0
    for item in snapshot.balances:
        if str(getattr(item, "asset", "")).upper() != "USDT":
            continue
        free_total += float(getattr(item, "free", 0.0) or 0.0)
        total_total += float(getattr(item, "total", 0.0) or 0.0)
    return free_total, total_total, None


@dataclass(frozen=True)
class _StrategyKpis:
    win_rate_pct: float | None
    max_dd_pct: float | None
    sharpe_proxy: float | None
    roi_pct: float | None
    health_class: str | None
    sample_size: int | None
    kpi_as_of: datetime | None


async def _resolve_strategy_kpis(
    *,
    session: AsyncSession,
    config: AutoTradeConfig,
    snapshot: StrategyHealthSnapshot | None,
    now: datetime,
    kpi_freshness: timedelta,
) -> _StrategyKpis:
    """Resolve a strategy's live KPIs (B4).

    Read the latest cron-written snapshot when fresh; recompute request-time
    (in-process ``compute_strategy_health``, no exchange call) for a *running*
    strategy whose snapshot is missing or stale; ``None`` for a stopped strategy
    with no snapshot — its metrics are frozen, so an absent snapshot means unknown.
    """
    snapshot_at = as_aware_utc(snapshot.computed_at) if snapshot is not None else None
    snapshot_is_stale = snapshot_at is None or (now - snapshot_at) > kpi_freshness

    if config.is_running and snapshot_is_stale:
        live = await compute_strategy_health(session=session, config_id=config.id)
        return _StrategyKpis(
            win_rate_pct=live.win_rate_pct,
            max_dd_pct=live.max_dd_pct,
            sharpe_proxy=live.sharpe_proxy,
            roi_pct=live.roi_pct,
            health_class=live.health_class,
            sample_size=live.sample_size,
            kpi_as_of=as_aware_utc(live.computed_at),
        )
    if snapshot is not None:
        return _StrategyKpis(
            win_rate_pct=snapshot.win_rate_pct,
            max_dd_pct=snapshot.max_dd_pct,
            sharpe_proxy=snapshot.sharpe_proxy,
            roi_pct=snapshot.roi_pct,
            health_class=snapshot.health_class,
            sample_size=snapshot.sample_size,
            kpi_as_of=snapshot_at,
        )
    return _StrategyKpis(None, None, None, None, None, None, None)


def merged_equity_max_dd_pct(all_trades: list[dict[str, Any]], base_usdt: float) -> float:
    """True portfolio drawdown (T12/W11a).

    Builds ONE equity curve over every strategy's closed trades, time-ordered, and
    returns its max drawdown %. Unlike the old worst-strategy proxy
    (``max`` of the per-strategy drawdowns), this catches portfolio-wide bleed that
    no single strategy breaches on its own. Returns 0.0 with no trades / no base.
    """
    # Drop trades with no exit time — a null would sort to the front and distort the
    # equity curve / drawdown (review suggestion). Closed trades normally have one.
    dated = [t for t in all_trades if t.get("exit_time")]
    if not dated or base_usdt <= 0:
        return 0.0
    ordered = sorted(dated, key=lambda trade: str(trade.get("exit_time")))
    curve = build_equity_curve(ordered, base_usdt)
    return calculate_equity_max_drawdown_pct(curve)


async def compute_merged_portfolio_dd_pct(
    *,
    session: AsyncSession,
    configs: list[AutoTradeConfig],
    cutoff: datetime,
) -> float:
    """Gather every config's closed trades over the window and compute the merged
    equity-curve drawdown (T12/W11a)."""
    all_trades: list[dict[str, Any]] = []
    base_usdt = 0.0
    for config in configs:
        all_trades.extend(
            await trades_for_health(
                session=session, config=config, config_id=config.id, cutoff=cutoff
            )
        )
        base_usdt += _normalization_base_usdt(config)
    return merged_equity_max_dd_pct(all_trades, base_usdt)


async def compute_portfolio(
    *,
    session: AsyncSession,
    auto_trade: AutoTradeService,
    trading: TradingService,
    user_id: int,
    fetch_balances: bool = True,
    include_merged_dd: bool = True,
) -> PortfolioSummary:
    """Build a :class:`PortfolioSummary` for every config the user owns.

    ``fetch_balances=False`` skips the parallel exchange calls — useful in
    tests that don't want to mock the adapter layer.

    ``include_merged_dd=False`` skips the merged-equity drawdown pass (a per-config
    trade scan). The 1-min SSE KPI push sets this False (review I5) — DD is slow-
    moving and refreshed by the request-time ``/auto-trade/portfolio`` poll; the
    push exists for live PnL. ``portfolio_max_dd_pct`` is then ``0.0`` in the push
    payload and the frontend preserves the last polled value.
    """

    configs = list(
        (
            await session.scalars(
                select(AutoTradeConfig)
                .where(AutoTradeConfig.user_id == user_id)
                .order_by(AutoTradeConfig.id.asc())
            )
        ).all()
    )
    if not configs:
        return PortfolioSummary(
            strategies=[],
            total_realized_pnl_usdt=0.0,
            total_unrealized_pnl_usdt=0.0,
            total_open_positions=0,
            total_running_strategies=0,
            portfolio_max_dd_pct=0.0,
        )

    account_ids = {config.account_id for config in configs}
    profile_ids = {config.profile_id for config in configs}

    accounts_rows = list(
        (
            await session.scalars(
                select(ExchangeCredential).where(
                    ExchangeCredential.user_id == user_id,
                    ExchangeCredential.id.in_(account_ids),
                )
            )
        ).all()
    )
    accounts_by_id = {row.id: row for row in accounts_rows}

    profiles_rows = list(
        (
            await session.scalars(
                select(PersonalAnalysisProfile).where(
                    PersonalAnalysisProfile.user_id == user_id,
                    PersonalAnalysisProfile.id.in_(profile_ids),
                )
            )
        ).all()
    )
    profiles_by_id = {row.id: row for row in profiles_rows}

    # Latest health snapshot per config, fetched in one query (review S2) — the
    # AC#7 KPIs (win_rate / max_dd / sharpe / roi); None until the guard cron writes one.
    snapshots_by_config = await latest_health_snapshots_for_configs(
        session=session, config_ids=[config.id for config in configs]
    )

    # Balances fetched in parallel — one slow exchange does not block others.
    balance_results: dict[int, tuple[float | None, float | None, str | None]] = {}
    if fetch_balances:
        targets = [config.account_id for config in configs]
        balance_outputs = await asyncio.gather(
            *(
                _fetch_usdt_balance(
                    trading=trading,
                    session=session,
                    user_id=user_id,
                    account_id=account_id,
                )
                for account_id in targets
            ),
            return_exceptions=False,
        )
        for account_id, payload in zip(targets, balance_outputs):
            balance_results[account_id] = payload

    total_realized = 0.0
    total_unrealized = 0.0
    total_open = 0
    total_running = 0
    entries: list[StrategyPortfolioEntry] = []

    # B4 — freshness window for the live-KPI recompute fallback.
    now = datetime.now(UTC)
    kpi_freshness = timedelta(seconds=get_settings().kpi_freshness_seconds)

    for config in configs:
        # Realized: exchange-accurate net (Σ realized − commission + funding) from
        # the synced ledger — pure DB, NO per-position exchange round-trips. This
        # both fixes the 20s+ loads (the old per-position-snapshot path issued a
        # live fetch_futures_trades for every closed position) and makes the number
        # match the per-account PnL card (the old path fell back to stored-price
        # estimates when a position had no mapped ledger rows).
        realized = await auto_trade._config_realized_net_usdt(
            session=session, config_id=config.id
        )

        # Open positions drive margin, live-unrealized and the count in ONE query.
        # Unrealized needs a live mark, so it is fetched only for OPEN positions
        # (bounded by open count) — never for the closed history.
        open_positions = list(
            (
                await session.scalars(
                    select(AutoTradePosition).where(
                        AutoTradePosition.user_id == user_id,
                        AutoTradePosition.config_id == config.id,
                        AutoTradePosition.status == POSITION_OPEN,
                    )
                )
            ).all()
        )
        open_positions_count = len(open_positions)

        # Margin used = sum of posted margin over open positions.
        # ``position_size_usdt`` is the *notional* the strategy deploys, so the
        # margin actually locked is ``notional / leverage`` — matching Binance
        # ``initialMargin = notional / leverage``.
        margin_used = 0.0
        unrealized = 0.0
        for position in open_positions:
            try:
                margin_used += float(position.position_size_usdt) / max(
                    float(position.leverage), 1.0
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            snapshot = await auto_trade.build_position_pnl_snapshot(
                session=session, user_id=user_id, position=position
            )
            value = snapshot.get("unrealized_pnl_usdt")
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                unrealized += float(value)

        total_realized += realized
        total_unrealized += unrealized
        total_open += open_positions_count
        if config.is_running:
            total_running += 1

        account = accounts_by_id.get(config.account_id)
        profile = profiles_by_id.get(config.profile_id)
        free, total, error = balance_results.get(config.account_id, (None, None, None))

        # B4 — resolve live KPIs: a fresh snapshot, a request-time recompute for a
        # stale running strategy, or None for a stopped strategy without a snapshot.
        kpis = await _resolve_strategy_kpis(
            session=session,
            config=config,
            snapshot=snapshots_by_config.get(config.id),
            now=now,
            kpi_freshness=kpi_freshness,
        )
        entries.append(
            StrategyPortfolioEntry(
                config_id=config.id,
                account_id=config.account_id,
                account_label=account.account_label if account is not None else "",
                exchange_name=account.exchange_name if account is not None else "",
                mode=account.mode if account is not None else "",
                lifecycle_stage=config.lifecycle_stage,
                strategy_name=config.strategy_name,
                profile_id=config.profile_id,
                profile_symbol=profile.symbol if profile is not None else None,
                is_running=bool(config.is_running),
                enabled=bool(config.enabled),
                open_positions_count=open_positions_count,
                margin_used_usdt=margin_used,
                realized_pnl_usdt=realized,
                unrealized_pnl_usdt=unrealized,
                balance_total_usdt=total,
                balance_free_usdt=free,
                last_started_at=config.last_started_at,
                last_stopped_at=config.last_stopped_at,
                balance_error=error,
                win_rate_pct=kpis.win_rate_pct,
                max_dd_pct=kpis.max_dd_pct,
                sharpe_proxy=kpis.sharpe_proxy,
                roi_pct=kpis.roi_pct,
                health_class=kpis.health_class,
                sample_size=kpis.sample_size,
                kpi_as_of=kpis.kpi_as_of,
            )
        )

    # T12 (W11a): true merged-equity portfolio drawdown over the last 30d, not the
    # worst single strategy's drawdown. Per-strategy max_dd_pct is still reported on
    # each entry above; this is the portfolio-level figure used by the DD halt guard.
    # Skipped on the high-frequency SSE push path (review I5).
    portfolio_max_dd = (
        await compute_merged_portfolio_dd_pct(
            session=session, configs=configs, cutoff=now - timedelta(days=30)
        )
        if include_merged_dd
        else 0.0
    )

    return PortfolioSummary(
        strategies=entries,
        total_realized_pnl_usdt=total_realized,
        total_unrealized_pnl_usdt=total_unrealized,
        total_open_positions=total_open,
        total_running_strategies=total_running,
        portfolio_max_dd_pct=portfolio_max_dd,
    )


