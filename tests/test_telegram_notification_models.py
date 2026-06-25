from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auto_trade_event import AutoTradeEvent
from app.models.base import Base
from app.models.telegram_notification_delivery import TelegramNotificationDelivery
from app.models.telegram_notification_settings import TelegramNotificationSettings
from app.models.user import User


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "telegram_models.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _create_user(session: AsyncSession, user_id: int = 1) -> User:
    user = User(id=user_id, email=f"u{user_id}@example.com", hashed_password="x", is_active=True)
    session.add(user)
    await session.commit()
    return user


async def test_settings_defaults_to_disabled_and_unlinked(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        await _create_user(session)
        session.add(TelegramNotificationSettings(user_id=1))
        await session.commit()

    async with db() as session:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == 1
            )
        )
    assert row is not None
    assert row.chat_id is None
    assert row.enabled is False
    assert row.notify_on_open is True
    assert row.notify_on_close is True
    assert row.notify_on_risk is False
    assert row.linked_at is None


async def test_delivery_keyed_by_event_id_with_pending_default(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        await _create_user(session)
        event = AutoTradeEvent(
            user_id=1,
            event_type="position_opened",
            level="info",
            payload={"symbol": "BTC/USDT"},
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        session.add(TelegramNotificationDelivery(event_id=event.id, user_id=1))
        await session.commit()
        event_id = event.id

    async with db() as session:
        row = await session.get(TelegramNotificationDelivery, event_id)
    assert row is not None
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.last_error is None
    assert row.sent_at is None
