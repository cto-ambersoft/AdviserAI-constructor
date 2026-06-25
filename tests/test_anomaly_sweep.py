"""Tests for the strategy-anomaly sweep (B6 — W12, P4-6).

The detector itself is covered by tests/test_anomaly_detector.py; here we test the
sweep orchestration — config selection, event emission, dedup cooldown and the
disabled-skip — with ``_load_anomaly_series`` patched to inject a known series.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_event import AutoTradeEvent
from app.models.auto_trade_risk_config import AutoTradeRiskConfig
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.services.auto_trade.service import AutoTradeService

_ANOMALOUS = ([1.0, -1.0] * 10)[:19] + [-50.0]
_FLAT = [1.0] * 30


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'anom.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _service(series: list[float]) -> AutoTradeService:
    service = AutoTradeService(trading_service=cast(Any, SimpleNamespace()))

    async def _fake_series(*, session: AsyncSession, config_id: int, limit: int) -> tuple[Any, Any]:
        return list(series), []

    service._load_anomaly_series = _fake_series  # type: ignore[method-assign]
    return service


async def _seed(session: AsyncSession, *, anomaly_enabled: bool, running: bool = True) -> int:
    user = User(email="a@example.com", hashed_password="x", is_active=True)
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
        is_running=running,
    )
    session.add(config)
    await session.flush()
    session.add(
        AutoTradeRiskConfig(config_id=config.id, anomaly_detection_enabled=anomaly_enabled)
    )
    await session.commit()
    await session.refresh(config)
    return config.id


async def _anomaly_events(session: AsyncSession, config_id: int) -> int:
    return await session.scalar(  # type: ignore[return-value]
        select(func.count())
        .select_from(AutoTradeEvent)
        .where(
            AutoTradeEvent.config_id == config_id,
            AutoTradeEvent.event_type == "strategy_anomaly_detected",
        )
    )


async def test_anomalous_series_emits_one_event(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config_id = await _seed(session, anomaly_enabled=True)
        stats = await _service(_ANOMALOUS).sweep_strategy_anomalies(session=session)
        assert stats == {"evaluated": 1, "alerted": 1, "errors": 0}
        assert await _anomaly_events(session, config_id) == 1


async def test_flat_series_emits_nothing(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config_id = await _seed(session, anomaly_enabled=True)
        stats = await _service(_FLAT).sweep_strategy_anomalies(session=session)
        assert stats == {"evaluated": 1, "alerted": 0, "errors": 0}
        assert await _anomaly_events(session, config_id) == 0


async def test_disabled_config_is_skipped(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config_id = await _seed(session, anomaly_enabled=False)
        stats = await _service(_ANOMALOUS).sweep_strategy_anomalies(session=session)
        assert stats["evaluated"] == 0
        assert await _anomaly_events(session, config_id) == 0


async def test_cooldown_dedup_suppresses_second_alert(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config_id = await _seed(session, anomaly_enabled=True)
        service = _service(_ANOMALOUS)
        await service.sweep_strategy_anomalies(session=session)
        # Second sweep within the cooldown must not emit a duplicate.
        stats = await service.sweep_strategy_anomalies(session=session)
        assert stats["alerted"] == 0
        assert await _anomaly_events(session, config_id) == 1
