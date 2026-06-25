"""Portfolio-DD watcher (B2 / P1-T2): auto-pause ALL of a user's strategies.

When the worst running-strategy drawdown breaches ``portfolio_dd_halt_threshold_pct``
the sweep flips every strategy off (``set_running_bulk``) and emits one user-level
``portfolio_dd_halt`` risk event. Ships behind ``portfolio_dd_halt_enabled`` (off by
default — real money). Health is mocked so the test never touches the exchange.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.auto_trade.service as service_mod
from app.core.config import Settings
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_event import AutoTradeEvent
from app.models.base import Base
from app.schemas.auto_trade import AutoTradeConfigUpsertRequest
from app.services.auto_trade.health import StrategyHealth
from app.services.auto_trade.service import AutoTradeService
from tests.test_auto_trade_service import (
    _create_profile_and_account,
    _seed_user_profile_and_account,
)


@pytest.fixture
async def auto_trade_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "portfolio_dd_guard.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _make_health(*, config_id: int, max_dd_pct: float) -> StrategyHealth:
    return StrategyHealth(
        config_id=config_id,
        window_days=30,
        sample_size=20,
        win_rate_pct=50.0,
        max_dd_pct=max_dd_pct,
        total_pnl_usdt=0.0,
        roi_pct=0.0,
        sharpe_proxy=0.0,
        stability_score=0.0,
        health_score=50.0,
        health_class="ok",
        computed_at=datetime.now(UTC),
    )


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, enabled: bool, threshold: float) -> None:
    monkeypatch.setattr(
        service_mod,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            portfolio_dd_halt_enabled=enabled,
            portfolio_dd_halt_threshold_pct=threshold,
        ),
    )


def _patch_health(monkeypatch: pytest.MonkeyPatch, dd_by_config: dict[int, float]) -> None:
    async def _fake(*, session: AsyncSession, config_id: int) -> StrategyHealth:
        return _make_health(config_id=config_id, max_dd_pct=dd_by_config.get(config_id, 0.0))

    monkeypatch.setattr(service_mod, "compute_strategy_health", _fake)


def _patch_portfolio_dd(monkeypatch: pytest.MonkeyPatch, dd_value: float) -> None:
    # T12: the guard now uses the merged-equity portfolio DD, not per-strategy health.
    import app.services.auto_trade.portfolio as portfolio_mod

    async def _fake(
        *, session: AsyncSession, configs: object, cutoff: object
    ) -> float:
        return dd_value

    monkeypatch.setattr(portfolio_mod, "compute_merged_portfolio_dd_pct", _fake)


async def _start_config(
    *,
    session: AsyncSession,
    service: AutoTradeService,
    user_id: int,
    profile_id: int,
    account_id: int,
) -> int:
    payload = AutoTradeConfigUpsertRequest(
        enabled=True,
        profile_id=profile_id,
        account_id=account_id,
        position_size_usdt=100.0,
        leverage=1,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
    )
    row = await service.upsert_config(session=session, user_id=user_id, payload=payload)
    await service.set_running(
        session=session, user_id=user_id, is_running=True, account_id=account_id
    )
    return row.id


async def _seed_two_running_strategies(
    session: AsyncSession, service: AutoTradeService
) -> tuple[int, int, int]:
    """Return (user_id, config_id_a, config_id_b) — two running strategies, one user."""
    user, profile_a, account_a = await _seed_user_profile_and_account(session)
    profile_b, account_b = await _create_profile_and_account(
        session, user_id=user.id, symbol="ETHUSDT", account_label="second"
    )
    config_a = await _start_config(
        session=session,
        service=service,
        user_id=user.id,
        profile_id=profile_a.id,
        account_id=account_a,
    )
    config_b = await _start_config(
        session=session,
        service=service,
        user_id=user.id,
        profile_id=profile_b.id,
        account_id=account_b,
    )
    return user.id, config_a, config_b


async def _running_flags(session: AsyncSession, user_id: int) -> list[bool]:
    rows = await session.scalars(
        select(AutoTradeConfig.is_running).where(AutoTradeConfig.user_id == user_id)
    )
    return list(rows)


@pytest.mark.asyncio
async def test_breach_halts_all_user_strategies_and_emits_event(
    auto_trade_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AutoTradeService()
    async with auto_trade_db() as session:
        user_id, config_a, config_b = await _seed_two_running_strategies(session, service)
        # Merged-equity portfolio DD 25% breaches the 10% threshold.
        _patch_portfolio_dd(monkeypatch, 25.0)
        _patch_settings(monkeypatch, enabled=True, threshold=10.0)

        stats = await service.sweep_portfolio_dd_guards(session=session)

        assert stats == {"users": 1, "halted": 1, "errors": 0}
        assert all(flag is False for flag in await _running_flags(session, user_id))

        events = list(
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.event_type == "portfolio_dd_halt")
            )
        )
        assert len(events) == 1
        evt = events[0]
        assert evt.user_id == user_id
        assert evt.config_id is None
        assert evt.payload["portfolio_dd_pct"] == 25.0
        assert evt.payload["basis"] == "merged_equity"
        assert evt.payload["paused_count"] == 2
        assert evt.payload["threshold_pct"] == 10.0


@pytest.mark.asyncio
async def test_no_breach_leaves_strategies_running(
    auto_trade_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AutoTradeService()
    async with auto_trade_db() as session:
        user_id, config_a, config_b = await _seed_two_running_strategies(session, service)
        _patch_health(monkeypatch, {config_a: 4.0, config_b: 8.0})  # both below 10%
        _patch_settings(monkeypatch, enabled=True, threshold=10.0)

        stats = await service.sweep_portfolio_dd_guards(session=session)

        assert stats == {"users": 1, "halted": 0, "errors": 0}
        assert all(flag is True for flag in await _running_flags(session, user_id))
        assert (
            await session.scalar(
                select(AutoTradeEvent).where(AutoTradeEvent.event_type == "portfolio_dd_halt")
            )
            is None
        )


@pytest.mark.asyncio
async def test_disabled_watcher_is_noop(
    auto_trade_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AutoTradeService()
    async with auto_trade_db() as session:
        user_id, config_a, config_b = await _seed_two_running_strategies(session, service)
        _patch_health(monkeypatch, {config_a: 99.0, config_b: 99.0})  # would breach if enabled
        _patch_settings(monkeypatch, enabled=False, threshold=10.0)

        stats = await service.sweep_portfolio_dd_guards(session=session)

        assert stats == {"users": 0, "halted": 0, "errors": 0}
        assert all(flag is True for flag in await _running_flags(session, user_id))


@pytest.mark.asyncio
async def test_idempotent_second_sweep_does_not_re_halt(
    auto_trade_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AutoTradeService()
    async with auto_trade_db() as session:
        user_id, config_a, config_b = await _seed_two_running_strategies(session, service)
        _patch_portfolio_dd(monkeypatch, 25.0)
        _patch_settings(monkeypatch, enabled=True, threshold=10.0)

        first = await service.sweep_portfolio_dd_guards(session=session)
        second = await service.sweep_portfolio_dd_guards(session=session)

        assert first["halted"] == 1
        # No running strategies left → nothing to evaluate or halt.
        assert second == {"users": 0, "halted": 0, "errors": 0}
        events = list(
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.event_type == "portfolio_dd_halt")
            )
        )
        assert len(events) == 1


@pytest.mark.asyncio
async def test_halt_persists_and_logs_critical_when_alert_emit_fails(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I1 — the halt commits via set_running_bulk before the alert event is emitted.
    If the emit fails, the strategies must stay paused (the safety action succeeded)
    and the lost alert must be LOUD (CRITICAL), never silent."""
    service = AutoTradeService()
    original_emit = service._emit_event

    async def _emit_or_fail(*, event_type: str, **kw: object) -> object:
        if event_type == "portfolio_dd_halt":
            raise RuntimeError("telegram event insert boom")
        return await original_emit(event_type=event_type, **kw)  # type: ignore[arg-type]

    async with auto_trade_db() as session:
        user_id, config_a, config_b = await _seed_two_running_strategies(session, service)
        _patch_portfolio_dd(monkeypatch, 25.0)
        _patch_settings(monkeypatch, enabled=True, threshold=10.0)
        monkeypatch.setattr(service, "_emit_event", _emit_or_fail)

        with caplog.at_level(logging.CRITICAL):
            stats = await service.sweep_portfolio_dd_guards(session=session)

        # The halt succeeded despite the lost alert.
        assert all(flag is False for flag in await _running_flags(session, user_id))
        assert stats["halted"] == 1
        assert stats["errors"] == 0  # emit failure is handled, not a user-level error
        # The alert event was not persisted (its emit raised + rolled back)...
        assert (
            await session.scalar(
                select(AutoTradeEvent).where(AutoTradeEvent.event_type == "portfolio_dd_halt")
            )
            is None
        )
        # ...but it was logged loudly.
        assert any(r.levelno == logging.CRITICAL for r in caplog.records)


@pytest.mark.asyncio
async def test_sweep_does_not_halt_on_non_positive_threshold(
    auto_trade_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1 defense-in-depth: even if a non-positive threshold reaches the sweep
    (bypassing Settings validation), it must NOT mass-halt — worst_dd seeds at 0.0
    and 0.0 < 0.0 is False, which would otherwise pause everything."""
    service = AutoTradeService()
    async with auto_trade_db() as session:
        user_id, config_a, config_b = await _seed_two_running_strategies(session, service)
        _patch_health(monkeypatch, {config_a: 0.0, config_b: 0.0})
        # SimpleNamespace bypasses the Field(gt=0) validator a real Settings enforces.
        monkeypatch.setattr(
            service_mod,
            "get_settings",
            lambda: SimpleNamespace(
                portfolio_dd_halt_enabled=True, portfolio_dd_halt_threshold_pct=0.0
            ),
        )

        stats = await service.sweep_portfolio_dd_guards(session=session)

        assert stats == {"users": 0, "halted": 0, "errors": 0}
        assert all(flag is True for flag in await _running_flags(session, user_id))


def test_portfolio_dd_sweep_cron_registered() -> None:
    # P1-T3 — the watcher runs every 5 minutes (UTC) via LabelScheduleSource.
    from app.worker import tasks as worker_tasks

    schedule = worker_tasks.evaluate_portfolio_dd_guards.labels["schedule"]
    assert any(entry.get("cron") == "*/5 * * * *" for entry in schedule)
    assert any(entry.get("schedule_id") == "portfolio_dd_every_5m" for entry in schedule)
