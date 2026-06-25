"""Regression: hydrate_active_positions re-tracks OPEN positions after restart.

Without this, the WS manager loses all references on API restart and TP/SL
fills on the exchange never reach ``MultiTPEngine`` — the SL never moves.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.services.auto_trade.service as service_module  # noqa: E402
from app.models.auto_trade_config import AutoTradeConfig  # noqa: E402
from app.models.auto_trade_position import AutoTradePosition  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.exchange import ExchangeCredential  # noqa: E402
from app.models.personal_analysis_profile import PersonalAnalysisProfile  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.auto_trade.service import AutoTradeService  # noqa: E402


class _FakeWSManager:
    """Drop-in for WebSocketManager that records track_position calls."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.tracked: list[Any] = []

    async def start(self) -> None:
        return None

    def track_position(self, position: Any) -> None:
        self.tracked.append(position)

    def is_connected(self) -> bool:
        return True

    def is_reconnecting(self) -> bool:
        return False

    async def _ensure_realtime_sl_pipeline(self, _position: Any) -> None:
        return None


@pytest.fixture
async def hydrate_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "hydrate.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_account_and_position(
    session: AsyncSession,
    *,
    state: str,
    label_suffix: str = "",
) -> int:
    user = User(
        email=f"hyd-{state}{label_suffix}@example.com",
        hashed_password="x",
        is_active=True,
    )
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
        last_triggered_at=None,
        last_completed_at=None,
    )
    session.add(profile)
    await session.flush()

    account = ExchangeCredential(
        user_id=user.id,
        exchange_name="bybit",
        account_label=f"main{label_suffix}",
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
        position_size_usdt=100.0,
        leverage=1,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
    )
    session.add(config)
    await session.flush()

    position = AutoTradePosition(
        user_id=user.id,
        config_id=config.id,
        profile_id=profile.id,
        account_id=account.id,
        symbol="BTC/USDT:USDT",
        side="LONG",  # check constraint requires uppercase
        entry_price=100_000.0,
        original_quantity=1.0,
        current_quantity=1.0,
        quantity=1.0,
        position_size_usdt=100.0,
        sl_price=98_000.0,
        tp_price=103_000.0,
        entry_confidence_pct=70.0,
        leverage=1,
        state=state,
        status="open" if state == "open" else "closed",
        tp_mode="single",
        tp_levels_json=[],
        sl_history_json=[],
        tp_history_json=[],
        active_watchers_json=[],
        adjustment_priority_json=["watcher", "trailing", "breakeven", "volatility"],
        transition_log_json=[],
        opened_at=datetime.now(UTC),
        sl_type="fixed",
    )
    session.add(position)
    await session.commit()
    return position.id


async def test_hydrate_active_positions_tracks_open_skips_closed(
    hydrate_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_managers: dict[str, _FakeWSManager] = {}

    def _factory(*_a: Any, **kwargs: Any) -> _FakeWSManager:
        manager = _FakeWSManager()
        fake_managers[str(kwargs.get("account_id"))] = manager
        return manager

    monkeypatch.setattr(service_module, "WebSocketManager", _factory)
    monkeypatch.setattr(service_module, "AsyncSessionFactory", hydrate_db)
    service_module._WS_MANAGER_REGISTRY.clear()

    service = AutoTradeService()
    monkeypatch.setattr(
        service,
        "_create_exchange_adapter",
        AsyncMock(return_value=AsyncMock()),
    )

    async with hydrate_db() as session:
        await _seed_account_and_position(session, state="open", label_suffix="-A")
        await _seed_account_and_position(session, state="closed", label_suffix="-B")

    hydrated = await service.hydrate_active_positions()

    assert hydrated == 1
    assert len(fake_managers) == 1
    manager = next(iter(fake_managers.values()))
    assert len(manager.tracked) == 1
    assert manager.tracked[0].symbol == "BTC/USDT:USDT"

    # Cleanup so subsequent tests do not see this leftover registry entry.
    service_module._WS_MANAGER_REGISTRY.clear()


async def test_hydrate_active_positions_is_idempotent(
    hydrate_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_managers: dict[str, _FakeWSManager] = {}

    def _factory(*_a: Any, **kwargs: Any) -> _FakeWSManager:
        manager = _FakeWSManager()
        fake_managers[str(kwargs.get("account_id"))] = manager
        return manager

    monkeypatch.setattr(service_module, "WebSocketManager", _factory)
    monkeypatch.setattr(service_module, "AsyncSessionFactory", hydrate_db)
    service_module._WS_MANAGER_REGISTRY.clear()

    service = AutoTradeService()
    monkeypatch.setattr(
        service,
        "_create_exchange_adapter",
        AsyncMock(return_value=AsyncMock()),
    )

    async with hydrate_db() as session:
        await _seed_account_and_position(session, state="open")

    first = await service.hydrate_active_positions()
    second = await service.hydrate_active_positions()

    assert first == 1
    assert second == 1  # tracked again — idempotent, not "no-op count zero"
    # Only ONE manager instance was created across the two calls (registry hit).
    assert len(fake_managers) == 1

    service_module._WS_MANAGER_REGISTRY.clear()
