"""T16a (W12e/AC#1): a live auto-trade strategy can be attached to a catalogue
forecast (provenance link to core's ``forecastId``). Persisted through upsert and
returned on read, so the trader's "attach to live strategy" round-trips.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auto_trade_config import AutoTradeConfig
from app.models.base import Base
from app.schemas.auto_trade import AutoTradeConfigUpsertRequest
from app.services.auto_trade.service import AutoTradeService
from tests.test_auto_trade_service import _seed_user_profile_and_account


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'forecast.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _payload(profile_id: int, account_id: int, **over: object) -> AutoTradeConfigUpsertRequest:
    base: dict[str, object] = dict(
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
    base.update(over)
    return AutoTradeConfigUpsertRequest(**base)


async def test_upsert_persists_attached_forecast_id(
    db: async_sessionmaker[AsyncSession],
) -> None:
    service = AutoTradeService()
    async with db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        row = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_payload(profile.id, account_id, attached_forecast_id="FC-btc-1h-001"),
        )
        fetched = await session.get(AutoTradeConfig, row.id)
        assert fetched is not None
        assert fetched.attached_forecast_id == "FC-btc-1h-001"


async def test_attached_forecast_id_defaults_to_none(
    db: async_sessionmaker[AsyncSession],
) -> None:
    service = AutoTradeService()
    async with db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        row = await service.upsert_config(
            session=session, user_id=user.id, payload=_payload(profile.id, account_id)
        )
        assert row.attached_forecast_id is None
