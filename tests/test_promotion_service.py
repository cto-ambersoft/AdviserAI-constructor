"""Integration tests for promote/demote/status service methods (B5 — W10, P4-2).

Uses an in-memory SQLite DB with the real models. ``compute_strategy_health`` is
patched to a controlled reading so the test isolates the FSM/gate/event wiring
from health computation (which has its own tests).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.auto_trade.service as svc
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_event import AutoTradeEvent
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.strategy_promotion_event import StrategyPromotionEvent
from app.models.user import User
from app.services.auto_trade.health import HEALTH_CLASS_INSUFFICIENT, StrategyHealth
from app.services.auto_trade.promotion import LifecycleStage
from app.services.auto_trade.service import AutoTradeService, PromotionGateError


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'promo.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _service() -> AutoTradeService:
    return AutoTradeService(trading_service=cast(Any, SimpleNamespace()))


async def _seed_config(
    session: AsyncSession,
    *,
    stage: LifecycleStage,
    sandbox_days_ago: float | None = None,
    is_running: bool = False,
) -> AutoTradeConfig:
    user = User(email="p@example.com", hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()
    profile = PersonalAnalysisProfile(
        user_id=user.id,
        symbol="BTCUSDT",
        query_prompt=None,
        agents={"twitterSentiment": True},
        agent_weights={"twitterSentiment": 1.0},
        interval_minutes=60,
        is_active=True,
        next_run_at=datetime.now(UTC),
    )
    session.add(profile)
    await session.flush()
    account = ExchangeCredential(
        user_id=user.id,
        exchange_name="bybit",
        account_label="main",
        mode="demo",
        encrypted_api_key="k",
        encrypted_api_secret="s",
        encrypted_passphrase=None,
    )
    session.add(account)
    await session.flush()
    entered = (
        datetime.now(UTC) - timedelta(days=sandbox_days_ago)
        if sandbox_days_ago is not None
        else None
    )
    config = AutoTradeConfig(
        user_id=user.id,
        profile_id=profile.id,
        account_id=account.id,
        enabled=True,
        is_running=is_running,
        lifecycle_stage=stage.value,
        sandbox_entered_at=entered,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


def _patch_health(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    base = dict(
        win_rate_pct=60.0,
        max_dd_pct=10.0,
        sample_size=30,
        health_class="healthy",
    )
    base.update(overrides)

    async def _fake(*, session: AsyncSession, config_id: int, **_: object) -> StrategyHealth:
        return StrategyHealth(
            config_id=config_id,
            window_days=30,
            sample_size=cast(int, base["sample_size"]),
            win_rate_pct=cast(float, base["win_rate_pct"]),
            max_dd_pct=cast(float, base["max_dd_pct"]),
            total_pnl_usdt=50.0,
            roi_pct=5.0,
            sharpe_proxy=1.0,
            stability_score=0.6,
            health_score=72.0,
            health_class=cast(str, base["health_class"]),
            computed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(svc, "compute_strategy_health", _fake)


async def _events(session: AsyncSession, config_id: int) -> list[str]:
    rows = await session.scalars(
        select(AutoTradeEvent.event_type).where(AutoTradeEvent.config_id == config_id)
    )
    return list(rows)


async def _promotion_events(
    session: AsyncSession, config_id: int
) -> list[StrategyPromotionEvent]:
    rows = await session.scalars(
        select(StrategyPromotionEvent)
        .where(StrategyPromotionEvent.config_id == config_id)
        .order_by(StrategyPromotionEvent.id)
    )
    return list(rows)


async def test_promote_records_lifecycle_audit_row(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # T19 (W10f): a promotion writes a first-class strategy_promotion_events row.
    _patch_health(monkeypatch)
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.SANDBOX, sandbox_days_ago=10)
        await _service().promote_strategy(
            session=session, user_id=config.user_id, config_id=config.id
        )
        events = await _promotion_events(session, config.id)
        assert len(events) == 1
        assert events[0].decision == "promoted"
        assert events[0].from_stage == "sandbox"
        assert events[0].to_stage == "live"
        assert events[0].kpi_snapshot_json is not None
        assert events[0].actor == "user"


async def test_demote_records_lifecycle_audit_row(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.LIVE)
        await _service().demote_strategy(
            session=session, user_id=config.user_id, config_id=config.id
        )
        events = await _promotion_events(session, config.id)
        assert [e.decision for e in events] == ["demoted"]
        assert events[0].from_stage == "live"
        assert events[0].to_stage == "sandbox"


async def test_gate_failure_records_lifecycle_audit_row(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, win_rate_pct=10.0)  # fails the gate
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.SANDBOX, sandbox_days_ago=10)
        with pytest.raises(PromotionGateError):
            await _service().promote_strategy(
                session=session, user_id=config.user_id, config_id=config.id
            )
        events = await _promotion_events(session, config.id)
        assert [e.decision for e in events] == ["gate_failed"]
        assert events[0].from_stage == "sandbox"
        assert events[0].to_stage == "sandbox"


async def test_promote_succeeds_when_gate_passes(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch)
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.SANDBOX, sandbox_days_ago=10)
        out = await _service().promote_strategy(
            session=session, user_id=config.user_id, config_id=config.id
        )
        assert out.lifecycle_stage == LifecycleStage.LIVE.value
        assert out.sandbox_entered_at is None
        assert "strategy_promoted" in await _events(session, config.id)


async def test_promoted_event_carries_gate_criteria_snapshot(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch)
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.SANDBOX, sandbox_days_ago=10)
        await _service().promote_strategy(
            session=session, user_id=config.user_id, config_id=config.id
        )
        event = await session.scalar(
            select(AutoTradeEvent).where(
                AutoTradeEvent.config_id == config.id,
                AutoTradeEvent.event_type == "strategy_promoted",
            )
        )
        assert event is not None
        names = {c["name"] for c in event.payload["criteria"]}
        assert names == {"min_trades", "min_sandbox_days", "min_win_rate", "max_dd"}
        assert all(c["passed"] for c in event.payload["criteria"])


async def test_promote_blocked_keeps_sandbox_and_raises(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Win rate below the 50% default → gate fails.
    _patch_health(monkeypatch, win_rate_pct=40.0)
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.SANDBOX, sandbox_days_ago=10)
        with pytest.raises(PromotionGateError) as exc:
            await _service().promote_strategy(
                session=session, user_id=config.user_id, config_id=config.id
            )
        assert "min_win_rate" in [c.name for c in exc.value.decision.failed]
        refreshed = await session.get(AutoTradeConfig, config.id)
        assert refreshed is not None and refreshed.lifecycle_stage == LifecycleStage.SANDBOX.value
        assert "promotion_gate_failed" in await _events(session, config.id)


async def test_promote_rejects_non_sandbox_stage(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch)
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.LIVE)
        with pytest.raises(svc.InvalidPromotionError):
            await _service().promote_strategy(
                session=session, user_id=config.user_id, config_id=config.id
            )


async def test_demote_returns_to_sandbox_and_stops(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.LIVE, is_running=True)
        out = await _service().demote_strategy(
            session=session, user_id=config.user_id, config_id=config.id
        )
        assert out.lifecycle_stage == LifecycleStage.SANDBOX.value
        assert out.is_running is False
        assert out.sandbox_entered_at is not None
        assert "strategy_demoted" in await _events(session, config.id)


async def test_get_promotion_status_reports_criteria(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch)
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.SANDBOX, sandbox_days_ago=10)
        status = await _service().get_promotion_status(
            session=session, user_id=config.user_id, config_id=config.id
        )
        assert status.can_promote is True
        assert {c.name for c in status.criteria} == {
            "min_trades",
            "min_sandbox_days",
            "min_win_rate",
            "max_dd",
        }
        assert status.sandbox_days >= 9.0


async def test_insufficient_sample_blocks_and_reports(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, sample_size=4, health_class=HEALTH_CLASS_INSUFFICIENT)
    async with db() as session:
        config = await _seed_config(session, stage=LifecycleStage.SANDBOX, sandbox_days_ago=30)
        status = await _service().get_promotion_status(
            session=session, user_id=config.user_id, config_id=config.id
        )
        assert status.can_promote is False
