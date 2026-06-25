"""Portfolio realized PnL comes from the synced ledger, not stored prices.

Regression for the prod bug where /auto-trade/portfolio showed a realized number
that disagreed with the exchange-accurate per-account PnL card (and took 20s+ via
a per-position fetch_futures_trades). The realized is now the ledger net
(Σ realized − commission + funding), pure DB.
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
from app.models.auto_trade_position import AutoTradePosition
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.exchange_trade_ledger import ExchangeTradeLedger
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.services.auto_trade.health import (
    HEALTH_CLASS_INSUFFICIENT,
    _net_realized_by_position,
    compute_strategy_health,
)
from app.services.auto_trade.service import AutoTradeService


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'realized.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _service() -> AutoTradeService:
    return AutoTradeService(trading_service=cast(Any, SimpleNamespace()))


async def _seed_config(session: AsyncSession) -> AutoTradeConfig:
    user = User(email="r@example.com", hashed_password="x", is_active=True)
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
        exchange_name="binance",
        account_label="acc",
        mode="real",
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
    await session.commit()
    await session.refresh(config)
    return config


def _fill(
    config: AutoTradeConfig,
    *,
    trade_id: str,
    side: str,
    realized_pnl: float,
    fee_cost: float,
    attributed: bool = True,
) -> ExchangeTradeLedger:
    now = datetime.now(UTC)
    return ExchangeTradeLedger(
        user_id=config.user_id,
        account_id=config.account_id,
        exchange_name="binance",
        symbol="BTCUSDT",
        exchange_trade_id=trade_id,
        side=side,
        price=100.0,
        amount=1.0,
        fee_cost=fee_cost,
        fee_currency="USDT",
        realized_pnl=realized_pnl,
        traded_at=now,
        ingested_at=now,
        # attributed=False → a manual/external fill (no config tag). Under the
        # one-account-per-strategy model it still counts toward the strategy.
        auto_trade_config_id=config.id if attributed else None,
    )


async def test_realized_uses_ledger_net_not_stored_prices(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config = await _seed_config(session)
        # Ledger: gross realized 59.03, commission 36.46, funding 0 → net 22.57.
        session.add(_fill(config, trade_id="t1", side="BUY", realized_pnl=0.0, fee_cost=18.23))
        session.add(
            _fill(config, trade_id="t2", side="SELL", realized_pnl=59.03, fee_cost=18.23)
        )
        # A stored closed position whose price-based PnL is wildly different — it
        # must NOT be the source when ledger fills exist.
        session.add(
            AutoTradePosition(
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                account_id=config.account_id,
                symbol="BTCUSDT",
                side="LONG",
                status="closed",
                entry_price=100.0,
                quantity=1.0,
                position_size_usdt=100.0,
                leverage=1,
                tp_price=110.0,
                sl_price=90.0,
                entry_confidence_pct=70.0,
                opened_at=datetime.now(UTC),
                closed_at=datetime.now(UTC),
                close_price=83.0,  # stored-price PnL = -17 (the old wrong number)
            )
        )
        await session.commit()

        net = await _service()._config_realized_net_usdt(
            session=session, config_id=config.id
        )
        assert net == pytest.approx(22.57, abs=0.01)


async def test_realized_includes_manual_account_fills(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # One sub-account per strategy: a manual (external, unattributed) fill on the
    # account still counts toward the strategy's realized PnL.
    async with db() as session:
        config = await _seed_config(session)
        # Bot fills: net 22.57 (gross 59.03, commission 36.46).
        session.add(_fill(config, trade_id="b1", side="BUY", realized_pnl=0.0, fee_cost=18.23))
        session.add(_fill(config, trade_id="b2", side="SELL", realized_pnl=59.03, fee_cost=18.23))
        # Manual fill on the SAME account, not tagged to the config: net +10.
        session.add(
            _fill(
                config,
                trade_id="m1",
                side="SELL",
                realized_pnl=10.0,
                fee_cost=0.0,
                attributed=False,
            )
        )
        await session.commit()
        net = await _service()._config_realized_net_usdt(
            session=session, config_id=config.id
        )
        assert net == pytest.approx(32.57, abs=0.01)  # 22.57 bot + 10.0 manual


async def test_health_kpis_account_wide_from_all_fills(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Health win-rate / sample / PnL come from ALL the account's closing fills
    # (auto + manual), not just bot positions.
    async with db() as session:
        config = await _seed_config(session)
        # 8 winners (+10) and 4 losers (-5), zero fees → net == realized.
        # Half are unattributed (manual) to prove account-wide scope.
        for i in range(8):
            session.add(
                _fill(
                    config,
                    trade_id=f"w{i}",
                    side="SELL",
                    realized_pnl=10.0,
                    fee_cost=0.0,
                    attributed=(i % 2 == 0),
                )
            )
        for i in range(4):
            session.add(
                _fill(
                    config,
                    trade_id=f"l{i}",
                    side="SELL",
                    realized_pnl=-5.0,
                    fee_cost=0.0,
                    attributed=(i % 2 == 0),
                )
            )
        await session.commit()

        health = await compute_strategy_health(session=session, config_id=config.id)
        assert health.sample_size == 12  # all closing fills, not bot positions
        assert health.health_class != HEALTH_CLASS_INSUFFICIENT
        assert round(health.win_rate_pct, 1) == round(8 / 12 * 100, 1)
        assert health.total_pnl_usdt == pytest.approx(60.0, abs=0.01)  # 8*10 - 4*5


async def test_health_net_by_position_uses_ledger_net(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config = await _seed_config(session)
        position = AutoTradePosition(
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            account_id=config.account_id,
            symbol="BTCUSDT",
            side="LONG",
            status="closed",
            entry_price=100.0,
            quantity=1.0,
            position_size_usdt=100.0,
            leverage=1,
            tp_price=110.0,
            sl_price=90.0,
            entry_confidence_pct=70.0,
            opened_at=datetime.now(UTC),
            closed_at=datetime.now(UTC),
            close_price=83.0,  # stored-price gross would be -17
        )
        session.add(position)
        await session.flush()  # need position.id for the fills' FK
        # Ledger fills mapped to this position: gross 59.03, commission 36.46.
        for trade_id, side, realized in (("t1", "BUY", 0.0), ("t2", "SELL", 59.03)):
            fill = _fill(config, trade_id=trade_id, side=side, realized_pnl=realized, fee_cost=18.23)
            fill.auto_trade_position_id = position.id
            session.add(fill)
        await session.commit()

        net = await _net_realized_by_position(session=session, positions=[position])
        assert net[position.id] == pytest.approx(22.57, abs=0.01)


async def test_health_net_by_position_empty_without_fills(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config = await _seed_config(session)
        position = AutoTradePosition(
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            account_id=config.account_id,
            symbol="BTCUSDT",
            side="LONG",
            status="closed",
            entry_price=100.0,
            quantity=1.0,
            position_size_usdt=100.0,
            leverage=1,
            tp_price=110.0,
            sl_price=90.0,
            entry_confidence_pct=70.0,
            opened_at=datetime.now(UTC),
            closed_at=datetime.now(UTC),
            close_price=105.0,
        )
        session.add(position)
        await session.commit()
        await session.refresh(position)
        # No fills → empty map → caller falls back to the stored-price gross.
        assert await _net_realized_by_position(session=session, positions=[position]) == {}


async def test_realized_falls_back_to_stored_prices_without_fills(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        config = await _seed_config(session)
        session.add(
            AutoTradePosition(
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                account_id=config.account_id,
                symbol="BTCUSDT",
                side="LONG",
                status="closed",
                entry_price=100.0,
                quantity=2.0,
                position_size_usdt=200.0,
                leverage=1,
                tp_price=110.0,
                sl_price=90.0,
                entry_confidence_pct=70.0,
                opened_at=datetime.now(UTC),
                closed_at=datetime.now(UTC),
                close_price=105.0,  # (105-100)*2 = +10
            )
        )
        await session.commit()
        net = await _service()._config_realized_net_usdt(
            session=session, config_id=config.id
        )
        assert net == pytest.approx(10.0, abs=0.01)
