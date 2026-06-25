"""Tests for the promotion-gate auto-eval sweep (B5 — W10, P4-3).

The gate decision is covered by tests/test_promotion_kpi_gate.py; here we test the
sweep — sandbox selection, promotion_ready emission, dedup cooldown — with
``compute_strategy_health`` patched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.auto_trade.service as svc
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_event import AutoTradeEvent
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.services.auto_trade.health import StrategyHealth
from app.services.auto_trade.promotion import LifecycleStage
from app.services.auto_trade.service import AutoTradeService


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gatesweep.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _patch_health(monkeypatch: pytest.MonkeyPatch, *, win_rate_pct: float) -> None:
    async def _fake(*, session: AsyncSession, config_id: int, **_: object) -> StrategyHealth:
        return StrategyHealth(
            config_id=config_id,
            window_days=30,
            sample_size=30,
            win_rate_pct=win_rate_pct,
            max_dd_pct=10.0,
            total_pnl_usdt=50.0,
            roi_pct=5.0,
            sharpe_proxy=1.0,
            stability_score=0.6,
            health_score=72.0,
            health_class="healthy",
            computed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(svc, "compute_strategy_health", _fake)


def _service() -> AutoTradeService:
    return AutoTradeService(trading_service=cast(Any, SimpleNamespace()))


async def _seed(session: AsyncSession, *, stage: LifecycleStage) -> int:
    user = User(email="g@example.com", hashed_password="x", is_active=True)
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
    config = AutoTradeConfig(
        user_id=user.id,
        profile_id=profile.id,
        account_id=account.id,
        enabled=True,
        is_running=True,
        lifecycle_stage=stage.value,
        sandbox_entered_at=datetime.now(UTC) - timedelta(days=10),
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config.id


async def _ready_events(session: AsyncSession, config_id: int) -> int:
    return await session.scalar(  # type: ignore[return-value]
        select(func.count())
        .select_from(AutoTradeEvent)
        .where(
            AutoTradeEvent.config_id == config_id,
            AutoTradeEvent.event_type == "promotion_ready",
        )
    )


async def test_passing_sandbox_strategy_emits_promotion_ready(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, win_rate_pct=60.0)
    async with db() as session:
        config_id = await _seed(session, stage=LifecycleStage.SANDBOX)
        stats = await _service().sweep_promotion_gates(session=session)
        assert stats == {"evaluated": 1, "ready": 1, "errors": 0}
        assert await _ready_events(session, config_id) == 1


async def test_failing_gate_emits_nothing(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, win_rate_pct=30.0)
    async with db() as session:
        config_id = await _seed(session, stage=LifecycleStage.SANDBOX)
        stats = await _service().sweep_promotion_gates(session=session)
        assert stats == {"evaluated": 1, "ready": 0, "errors": 0}
        assert await _ready_events(session, config_id) == 0


async def test_non_sandbox_stage_is_skipped(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, win_rate_pct=60.0)
    async with db() as session:
        config_id = await _seed(session, stage=LifecycleStage.LIVE)
        stats = await _service().sweep_promotion_gates(session=session)
        assert stats["evaluated"] == 0
        assert await _ready_events(session, config_id) == 0


async def test_cooldown_dedup_suppresses_second_ready(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, win_rate_pct=60.0)
    async with db() as session:
        config_id = await _seed(session, stage=LifecycleStage.SANDBOX)
        service = _service()
        await service.sweep_promotion_gates(session=session)
        stats = await service.sweep_promotion_gates(session=session)
        assert stats["ready"] == 0
        assert await _ready_events(session, config_id) == 1
