"""Endpoint tests for promote/demote/promotion-status (B5 — W10, P4-2 slice 3).

Exercises routing, step-up gating and error mapping. The service logic itself is
covered by tests/test_promotion_service.py; ``compute_strategy_health`` is patched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.auto_trade.service as svc
from app.api.deps import get_current_user, require_step_up
from app.db.session import get_db_session
from app.main import app
from app.models.auto_trade_config import AutoTradeConfig
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.services.auto_trade.health import StrategyHealth
from app.services.auto_trade.promotion import LifecycleStage


@pytest.fixture
async def endpoints_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'promo_ep.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _overrides(endpoints_db: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async def _db() -> AsyncIterator[AsyncSession]:
        async with endpoints_db() as session:
            yield session

    async def _user() -> User:
        return User(id=1, email="ep@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_step_up] = _user
    yield
    for dep in (get_db_session, get_current_user, require_step_up):
        app.dependency_overrides.pop(dep, None)


@pytest.fixture(autouse=True)
def _patch_health(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(*, session: AsyncSession, config_id: int, **_: object) -> StrategyHealth:
        return StrategyHealth(
            config_id=config_id,
            window_days=30,
            sample_size=30,
            win_rate_pct=getattr(_patch_health, "win_rate", 60.0),
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


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    stage: LifecycleStage,
    sandbox_days_ago: float | None = 10.0,
) -> int:
    async with factory() as session:
        user = User(id=1, email="ep@example.com", hashed_password="x", is_active=True)
        session.add(user)
        profile = PersonalAnalysisProfile(
            user_id=1,
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
            user_id=1,
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
            user_id=1,
            profile_id=profile.id,
            account_id=account.id,
            enabled=True,
            is_running=stage is LifecycleStage.LIVE,
            lifecycle_stage=stage.value,
            sandbox_entered_at=entered,
        )
        session.add(config)
        await session.commit()
        await session.refresh(config)
        return config.id


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_BASE = "/api/v1/live/auto-trade/strategies"


async def test_promote_endpoint_returns_live(
    endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    _patch_health.win_rate = 60.0  # type: ignore[attr-defined]
    config_id = await _seed(endpoints_db, stage=LifecycleStage.SANDBOX)
    async with _client() as client:
        resp = await client.post(f"{_BASE}/{config_id}/promote")
    assert resp.status_code == 200
    assert resp.json()["lifecycle_stage"] == "live"


async def test_promote_endpoint_422_on_gate_fail(
    endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    _patch_health.win_rate = 30.0  # type: ignore[attr-defined]
    config_id = await _seed(endpoints_db, stage=LifecycleStage.SANDBOX)
    async with _client() as client:
        resp = await client.post(f"{_BASE}/{config_id}/promote")
    assert resp.status_code == 422
    body = resp.json()["detail"]
    assert any(c["name"] == "min_win_rate" for c in body["failed"])


async def test_demote_endpoint_returns_sandbox(
    endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    config_id = await _seed(endpoints_db, stage=LifecycleStage.LIVE, sandbox_days_ago=None)
    async with _client() as client:
        resp = await client.post(f"{_BASE}/{config_id}/demote")
    assert resp.status_code == 200
    assert resp.json()["lifecycle_stage"] == "sandbox"


async def test_demote_endpoint_409_when_not_live(
    endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    config_id = await _seed(endpoints_db, stage=LifecycleStage.SANDBOX)
    async with _client() as client:
        resp = await client.post(f"{_BASE}/{config_id}/demote")
    assert resp.status_code == 409


async def test_promotion_status_endpoint(
    endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    _patch_health.win_rate = 60.0  # type: ignore[attr-defined]
    config_id = await _seed(endpoints_db, stage=LifecycleStage.SANDBOX)
    async with _client() as client:
        resp = await client.get(f"{_BASE}/{config_id}/promotion-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["can_promote"] is True
    assert {c["name"] for c in data["criteria"]} == {
        "min_trades",
        "min_sandbox_days",
        "min_win_rate",
        "max_dd",
    }
