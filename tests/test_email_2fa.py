"""Email-2FA enrollment model (E1) — descriptive model, no behaviour yet."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.user import User
from app.models.user_email_2fa import UserEmail2FA


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'email_2fa.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(User(id=1, email="e2fa@x.io", hashed_password="x", is_active=True))
            session.add(User(id=2, email="e2fb@x.io", hashed_password="x", is_active=True))
            await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_model_defaults_to_unconfirmed(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        session.add(UserEmail2FA(user_id=1))
        await session.commit()
        row = await session.scalar(select(UserEmail2FA).where(UserEmail2FA.user_id == 1))
        assert row is not None
        assert row.confirmed_at is None  # inactive until verified
        assert row.failed_attempts == 0
        assert row.locked_until is None
        assert row.created_at is not None


async def test_user_id_is_unique(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        session.add(UserEmail2FA(user_id=1))
        await session.commit()
    async with db() as session:
        session.add(UserEmail2FA(user_id=1))
        with pytest.raises(IntegrityError):
            await session.commit()
