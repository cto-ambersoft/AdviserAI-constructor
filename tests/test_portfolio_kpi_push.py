"""T15 (W12g): the Live Monitor must receive KPI numbers via SSE, not 30s polling.

A cron pushes a ``portfolio_kpi`` SSE event per user with running strategies,
carrying the same PortfolioSummary shape the /auto-trade/portfolio endpoint returns,
so the frontend updates KPIs from the stream.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.auto_trade.portfolio as portfolio_mod
import app.services.auto_trade.service as service_mod
from app.models.base import Base
from app.services.auto_trade.portfolio import PortfolioSummary
from app.services.auto_trade.service import AutoTradeService
from tests.test_auto_trade_service import _insert_config, _seed_user_profile_and_account


@pytest.fixture
async def auto_trade_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kpi_push.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _empty_summary() -> PortfolioSummary:
    return PortfolioSummary(
        strategies=[],
        total_realized_pnl_usdt=12.5,
        total_unrealized_pnl_usdt=-3.0,
        total_open_positions=1,
        total_running_strategies=1,
        portfolio_max_dd_pct=4.2,
    )


async def test_push_emits_portfolio_kpi_for_running_users(
    auto_trade_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AutoTradeService()
    published: list[dict[str, object]] = []

    async def _fake_compute(**_: object) -> PortfolioSummary:
        return _empty_summary()

    async def _fake_publish(*, user_id: int, event_type: str, **kw: object) -> None:
        published.append({"user_id": user_id, "event_type": event_type, **kw})

    monkeypatch.setattr(portfolio_mod, "compute_portfolio", _fake_compute)
    monkeypatch.setattr(service_mod, "publish_user_event", _fake_publish)

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        await session.commit()

        stats = await service.push_portfolio_kpis(session=session)

    assert stats["pushed"] == 1
    assert len(published) == 1
    evt = published[0]
    assert evt["event_type"] == "portfolio_kpi"
    assert evt["user_id"] == user.id
    payload = evt["payload"]
    assert isinstance(payload, dict)
    assert payload["total_realized_pnl_usdt"] == 12.5
    assert payload["portfolio_max_dd_pct"] == 4.2
    assert payload["strategies"] == []


async def test_push_skips_users_without_running_strategies(
    auto_trade_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AutoTradeService()
    published: list[object] = []

    async def _fake_publish(**kw: object) -> None:
        published.append(kw)

    monkeypatch.setattr(service_mod, "publish_user_event", _fake_publish)

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )  # is_running defaults False
        await session.commit()

        stats = await service.push_portfolio_kpis(session=session)

    assert stats == {"users": 0, "pushed": 0}
    assert published == []


def test_portfolio_kpi_cron_registered() -> None:
    # T15: KPI push runs every minute via LabelScheduleSource.
    from app.worker import tasks as worker_tasks

    schedule = worker_tasks.push_portfolio_kpis.labels["schedule"]
    assert any(entry.get("cron") == "* * * * *" for entry in schedule)
    assert any(entry.get("schedule_id") == "portfolio_kpi_every_1m" for entry in schedule)
