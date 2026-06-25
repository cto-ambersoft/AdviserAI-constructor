"""P4-4: non-live strategies may only run on a *demo* account.

Sandbox strategies accumulate their KPI track record on a demo account (real
demo trades → compute_strategy_health → KPI gate); they must be promoted through
the gate (step-up) before they can run on a *real* account. A non-live strategy
can never trade real money.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auto_trade_config import AutoTradeConfig
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.services.auto_trade.promotion import LifecycleStage
from app.services.auto_trade.service import AutoTradeService


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _service() -> AutoTradeService:
    return AutoTradeService(trading_service=cast(Any, SimpleNamespace()))


async def _seed(
    session: AsyncSession, *, stage: LifecycleStage, mode: str = "demo"
) -> tuple[int, int]:
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
        mode=mode,
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
        is_running=False,
        lifecycle_stage=stage.value,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return user.id, account.id


async def test_new_config_defaults_to_sandbox(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # T8 (W10e): fail-safe — a config created without an explicit stage must NOT
    # land in 'live' (real money). New strategies start in sandbox and earn their
    # way to live through the KPI gate.
    async with db() as session:
        user = User(email="d@example.com", hashed_password="x", is_active=True)
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
            user_id=user.id, profile_id=profile.id, account_id=account.id, enabled=True
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)
        assert config.lifecycle_stage == LifecycleStage.SANDBOX.value


async def test_cannot_start_sandbox_on_real_account(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        user_id, account_id = await _seed(session, stage=LifecycleStage.SANDBOX, mode="real")
        with pytest.raises(ValueError, match="may only run on a demo account"):
            await _service().set_running(
                session=session, user_id=user_id, is_running=True, account_id=account_id
            )


async def test_can_start_sandbox_on_demo_account(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # The chosen design: a sandbox strategy runs on demo to build its track record.
    async with db() as session:
        user_id, account_id = await _seed(session, stage=LifecycleStage.SANDBOX, mode="demo")
        out = await _service().set_running(
            session=session, user_id=user_id, is_running=True, account_id=account_id
        )
        assert out.is_running is True


async def test_can_start_live_strategy_on_real_account(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        user_id, account_id = await _seed(session, stage=LifecycleStage.LIVE, mode="real")
        out = await _service().set_running(
            session=session, user_id=user_id, is_running=True, account_id=account_id
        )
        assert out.is_running is True


async def test_stopping_non_live_strategy_is_allowed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Stopping is always safe — only *starting* on a real account is blocked.
    async with db() as session:
        user_id, account_id = await _seed(session, stage=LifecycleStage.SANDBOX, mode="real")
        out = await _service().set_running(
            session=session, user_id=user_id, is_running=False, account_id=account_id
        )
        assert out.is_running is False
