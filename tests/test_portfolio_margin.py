"""T11 — portfolio ``margin_used_usdt`` must be margin, not notional.

``position_size_usdt`` is the *notional* the strategy deploys (execution sizes
``quantity = position_size_usdt / price``), so the margin actually posted is
``notional / leverage`` — confirmed against Binance position risk
(``initialMargin = notional / leverage``). The portfolio aggregation previously
summed ``position_size_usdt`` directly and labelled it margin, overstating it by
the leverage factor.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.schemas.exchange_trading import SpotBalancesRead
from app.services.auto_trade.health import StrategyHealth, record_health_snapshot
from app.services.auto_trade.portfolio import compute_portfolio
from app.services.auto_trade.service import AutoTradeService
from app.services.execution.trading_service import TradingService


class _NoopTradingService:
    """No exchange round-trips: open positions have no live mark in this test."""

    async def fetch_futures_position(self, **_: object) -> None:
        return None

    async def fetch_futures_trades(self, **_: object) -> list[object]:
        return []

    async def get_spot_balances(self, **_: object) -> SpotBalancesRead:
        return SpotBalancesRead(account_id=0, exchange_name="binance", mode="demo", balances=[])


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'margin.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as s:
            yield s
    finally:
        await engine.dispose()


async def test_margin_used_is_notional_divided_by_leverage(session: AsyncSession) -> None:
    # Arrange: one strategy with a single 10x open position of 1000 USDT notional.
    user = User(id=1, email="m@example.com", hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()
    profile = PersonalAnalysisProfile(
        user_id=1,
        symbol="BTCUSDT",
        query_prompt=None,
        agents={"twitterSentiment": True},
        agent_weights={"twitterSentiment": 1.0},
        interval_minutes=60,
        is_active=True,
        next_run_at=datetime.now(UTC),
        last_triggered_at=None,
        last_completed_at=None,
    )
    session.add(profile)
    credential = ExchangeCredential(
        user_id=1,
        exchange_name="binance",
        account_label="sub",
        mode="demo",
        encrypted_api_key="k",
        encrypted_api_secret="s",
        encrypted_passphrase=None,
        api_key_hash=None,
    )
    session.add(credential)
    await session.flush()
    config = AutoTradeConfig(
        user_id=1,
        profile_id=profile.id,
        account_id=credential.id,
        enabled=True,
        is_running=False,
        position_size_usdt=1000.0,
        leverage=10,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
    )
    session.add(config)
    await session.flush()
    session.add(
        AutoTradePosition(
            user_id=1,
            config_id=config.id,
            profile_id=profile.id,
            account_id=credential.id,
            symbol="BTC/USDT:USDT",
            side="LONG",
            status="open",
            entry_price=100.0,
            quantity=10.0,  # 1000 notional / 100 price
            position_size_usdt=1000.0,
            leverage=10,
            tp_price=102.0,
            sl_price=99.0,
            entry_confidence_pct=70.0,
            opened_at=datetime.now(UTC),
            raw_open_order={},
            raw_close_order={},
        )
    )
    await session.commit()

    auto_trade = AutoTradeService()
    auto_trade._trading = _NoopTradingService()  # type: ignore[assignment]

    async def _noop_snapshot_sync(**_: object) -> None:
        # Without a live exchange the snapshot sync would close the position as
        # "missing on exchange"; keep it open so the margin aggregation sees it.
        return None

    auto_trade._sync_positions_snapshot_for_user = _noop_snapshot_sync  # type: ignore[assignment]

    # Act
    summary = await compute_portfolio(
        session=session,
        auto_trade=auto_trade,
        trading=TradingService(),
        user_id=1,
        fetch_balances=False,
    )

    # Assert: margin = 1000 notional / 10x = 100 USDT, not the 1000 notional.
    assert len(summary.strategies) == 1
    assert summary.strategies[0].margin_used_usdt == pytest.approx(100.0)


async def _seed_one_strategy(
    session: AsyncSession, *, is_running: bool = True
) -> AutoTradeConfig:
    user = User(id=1, email="kpi@example.com", hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()
    profile = PersonalAnalysisProfile(
        user_id=1,
        symbol="BTCUSDT",
        query_prompt=None,
        agents={"twitterSentiment": True},
        agent_weights={"twitterSentiment": 1.0},
        interval_minutes=60,
        is_active=True,
        next_run_at=datetime.now(UTC),
        last_triggered_at=None,
        last_completed_at=None,
    )
    session.add(profile)
    credential = ExchangeCredential(
        user_id=1,
        exchange_name="binance",
        account_label="sub",
        mode="demo",
        encrypted_api_key="k",
        encrypted_api_secret="s",
        encrypted_passphrase=None,
        api_key_hash=None,
    )
    session.add(credential)
    await session.flush()
    config = AutoTradeConfig(
        user_id=1,
        profile_id=profile.id,
        account_id=credential.id,
        enabled=True,
        is_running=True,
        position_size_usdt=100.0,
        leverage=1,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
    )
    config.is_running = is_running
    session.add(config)
    await session.flush()
    return config


def _build_portfolio_service() -> AutoTradeService:
    auto_trade = AutoTradeService()
    auto_trade._trading = _NoopTradingService()  # type: ignore[assignment]

    async def _noop_snapshot_sync(**_: object) -> None:
        return None

    auto_trade._sync_positions_snapshot_for_user = _noop_snapshot_sync  # type: ignore[assignment]
    return auto_trade


async def test_portfolio_carries_live_kpis_from_latest_snapshot(session: AsyncSession) -> None:
    """T3.2 — each strategy surfaces win_rate/max_dd/sharpe/roi from its latest
    health snapshot, and the portfolio carries the worst per-strategy max-DD."""
    config = await _seed_one_strategy(session)
    health = StrategyHealth(
        config_id=config.id,
        window_days=30,
        sample_size=12,
        win_rate_pct=58.0,
        max_dd_pct=17.5,
        total_pnl_usdt=42.0,
        roi_pct=42.0,
        sharpe_proxy=1.3,
        stability_score=0.6,
        health_score=72.0,
        health_class="healthy",
        computed_at=datetime.now(UTC),
    )
    await record_health_snapshot(session=session, health=health, user_id=1)
    await session.commit()

    summary = await compute_portfolio(
        session=session,
        auto_trade=_build_portfolio_service(),
        trading=TradingService(),
        user_id=1,
        fetch_balances=False,
    )

    assert len(summary.strategies) == 1
    entry = summary.strategies[0]
    assert entry.win_rate_pct == pytest.approx(58.0)
    assert entry.max_dd_pct == pytest.approx(17.5)
    assert entry.sharpe_proxy == pytest.approx(1.3)
    assert entry.roi_pct == pytest.approx(42.0)
    assert entry.health_class == "healthy"
    assert entry.sample_size == 12
    # T12: portfolio_max_dd_pct is now merged-equity (from closed trades), decoupled
    # from the per-strategy snapshot max_dd — 0.0 here since no trades are seeded.
    assert summary.portfolio_max_dd_pct == 0.0


async def test_stopped_strategy_kpis_are_none_without_snapshot(session: AsyncSession) -> None:
    """B4 — a STOPPED strategy with no snapshot carries None KPIs (its metrics are
    frozen, so there is nothing to recompute live); portfolio DD = 0."""
    await _seed_one_strategy(session, is_running=False)
    await session.commit()

    summary = await compute_portfolio(
        session=session,
        auto_trade=_build_portfolio_service(),
        trading=TradingService(),
        user_id=1,
        fetch_balances=False,
    )

    entry = summary.strategies[0]
    assert entry.win_rate_pct is None
    assert entry.max_dd_pct is None
    assert entry.sharpe_proxy is None
    assert entry.roi_pct is None
    assert entry.health_class is None
    assert entry.sample_size is None
    assert entry.kpi_as_of is None
    assert summary.portfolio_max_dd_pct == 0.0


def _make_health(*, config_id: int, max_dd_pct: float, computed_at: datetime) -> StrategyHealth:
    return StrategyHealth(
        config_id=config_id,
        window_days=30,
        sample_size=8,
        win_rate_pct=55.0,
        max_dd_pct=max_dd_pct,
        total_pnl_usdt=5.0,
        roi_pct=5.0,
        sharpe_proxy=0.9,
        stability_score=0.4,
        health_score=60.0,
        health_class="warning",
        computed_at=computed_at,
    )


async def test_running_strategy_without_snapshot_recomputes_live_kpis(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4 — a freshly-started (running) strategy with no snapshot yet recomputes
    live KPIs request-time instead of showing None."""
    config = await _seed_one_strategy(session, is_running=True)
    await session.commit()

    live = _make_health(config_id=config.id, max_dd_pct=12.0, computed_at=datetime.now(UTC))
    seen: dict[str, int] = {}

    async def _fake(*, session: AsyncSession, config_id: int) -> StrategyHealth:
        seen["config_id"] = config_id
        return live

    monkeypatch.setattr("app.services.auto_trade.portfolio.compute_strategy_health", _fake)

    summary = await compute_portfolio(
        session=session,
        auto_trade=_build_portfolio_service(),
        trading=TradingService(),
        user_id=1,
        fetch_balances=False,
    )

    entry = summary.strategies[0]
    assert seen["config_id"] == config.id  # recompute happened
    assert entry.win_rate_pct == pytest.approx(55.0)
    assert entry.max_dd_pct == pytest.approx(12.0)
    assert entry.sharpe_proxy == pytest.approx(0.9)
    assert entry.roi_pct == pytest.approx(5.0)
    assert entry.health_class == "warning"
    assert entry.sample_size == 8
    assert entry.kpi_as_of is not None
    assert summary.portfolio_max_dd_pct == 0.0  # T12: merged-equity, no trades seeded


async def test_fresh_snapshot_is_used_without_recompute(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4 — a running strategy whose latest snapshot is fresh (within
    kpi_freshness_seconds) reads the snapshot and does NOT recompute."""
    config = await _seed_one_strategy(session, is_running=True)
    await record_health_snapshot(
        session=session,
        health=_make_health(config_id=config.id, max_dd_pct=17.5, computed_at=datetime.now(UTC)),
        user_id=1,
    )
    await session.commit()

    recompute = AsyncMock()
    monkeypatch.setattr("app.services.auto_trade.portfolio.compute_strategy_health", recompute)

    summary = await compute_portfolio(
        session=session,
        auto_trade=_build_portfolio_service(),
        trading=TradingService(),
        user_id=1,
        fetch_balances=False,
    )

    recompute.assert_not_called()
    entry = summary.strategies[0]
    assert entry.max_dd_pct == pytest.approx(17.5)
    assert entry.kpi_as_of is not None


async def test_snapshot_within_window_is_used_without_recompute(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5 — a snapshot aged but still inside kpi_freshness_seconds (600s default) is
    'fresh': it is read, not recomputed (covers the gap between now and the boundary)."""
    config = await _seed_one_strategy(session, is_running=True)
    await record_health_snapshot(
        session=session,
        health=_make_health(
            config_id=config.id,
            max_dd_pct=14.0,
            computed_at=datetime.now(UTC) - timedelta(seconds=540),  # < 600s window
        ),
        user_id=1,
    )
    await session.commit()

    recompute = AsyncMock()
    monkeypatch.setattr("app.services.auto_trade.portfolio.compute_strategy_health", recompute)

    summary = await compute_portfolio(
        session=session,
        auto_trade=_build_portfolio_service(),
        trading=TradingService(),
        user_id=1,
        fetch_balances=False,
    )

    recompute.assert_not_called()
    assert summary.strategies[0].max_dd_pct == pytest.approx(14.0)


async def test_stale_snapshot_triggers_live_recompute(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4 — a running strategy whose latest snapshot is older than
    kpi_freshness_seconds recomputes live (shows the fresh value, not the stale one)."""
    config = await _seed_one_strategy(session, is_running=True)
    await record_health_snapshot(
        session=session,
        health=_make_health(
            config_id=config.id,
            max_dd_pct=5.0,
            computed_at=datetime.now(UTC) - timedelta(seconds=1200),  # > 600s window
        ),
        user_id=1,
    )
    await session.commit()

    live = _make_health(config_id=config.id, max_dd_pct=22.0, computed_at=datetime.now(UTC))

    async def _fake(*, session: AsyncSession, config_id: int) -> StrategyHealth:
        return live

    monkeypatch.setattr("app.services.auto_trade.portfolio.compute_strategy_health", _fake)

    summary = await compute_portfolio(
        session=session,
        auto_trade=_build_portfolio_service(),
        trading=TradingService(),
        user_id=1,
        fetch_balances=False,
    )

    entry = summary.strategies[0]
    assert entry.max_dd_pct == pytest.approx(22.0)  # live value, not the stale 5.0
    assert summary.portfolio_max_dd_pct == 0.0  # T12: merged-equity, no trades seeded


async def test_include_merged_dd_false_skips_the_dd_scan(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I5: the 1-min SSE push path passes include_merged_dd=False to skip the
    # per-config trade scan; portfolio_max_dd_pct is then 0.0 (filled by the poll).
    import app.services.auto_trade.portfolio as portfolio_mod

    async def _boom(**_: object) -> float:
        raise AssertionError("merged DD must not be computed when include_merged_dd=False")

    monkeypatch.setattr(portfolio_mod, "compute_merged_portfolio_dd_pct", _boom)
    await _seed_one_strategy(session, is_running=True)

    summary = await compute_portfolio(
        session=session,
        auto_trade=_build_portfolio_service(),
        trading=TradingService(),
        user_id=1,
        fetch_balances=False,
        include_merged_dd=False,
    )
    assert summary.portfolio_max_dd_pct == 0.0
