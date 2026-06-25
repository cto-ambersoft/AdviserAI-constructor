"""E2: the two_factor unifier — has_second_factor / available_factors.

Covers TOTP-only, email-only, both, neither, and Resend-off (email factor must
disappear). These are the gate's single source of truth, so they get dedicated tests.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pyotp
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.base import Base
from app.models.user import User
from app.models.user_email_2fa import UserEmail2FA
from app.services import two_factor
from app.services.totp import TotpService


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'two_factor.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(User(id=1, email="tf@x.io", hashed_password="x", is_active=True))
            await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def resend_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    monkeypatch.setattr(get_settings(), "email_from", "no-reply@ambersoft.llc")


async def _enroll_totp(session: AsyncSession) -> None:
    service = TotpService()
    result = await service.enroll(session=session, user_id=1, account_name="tf@x.io")
    await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())


async def _confirm_email_2fa(session: AsyncSession) -> None:
    from datetime import UTC, datetime

    session.add(UserEmail2FA(user_id=1, confirmed_at=datetime.now(UTC)))
    await session.commit()


async def test_neither_factor(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        assert await two_factor.available_factors(session=session, user_id=1) == set()
        assert await two_factor.has_second_factor(session=session, user_id=1) is False


async def test_totp_only(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await _enroll_totp(session)
        assert await two_factor.available_factors(session=session, user_id=1) == {"totp"}
        assert await two_factor.has_second_factor(session=session, user_id=1) is True


async def test_email_only(db: async_sessionmaker[AsyncSession], resend_on: None) -> None:
    async with db() as session:
        await _confirm_email_2fa(session)
        assert await two_factor.available_factors(session=session, user_id=1) == {"email"}
        assert await two_factor.has_second_factor(session=session, user_id=1) is True


async def test_both_factors(db: async_sessionmaker[AsyncSession], resend_on: None) -> None:
    async with db() as session:
        await _enroll_totp(session)
        await _confirm_email_2fa(session)
        assert await two_factor.available_factors(session=session, user_id=1) == {
            "totp",
            "email",
        }


async def test_email_factor_hidden_when_resend_off(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Confirmed email enrollment, but Resend not configured → email is not usable.
    async with db() as session:
        await _confirm_email_2fa(session)
        assert await two_factor.available_factors(session=session, user_id=1) == set()
        assert await two_factor.has_second_factor(session=session, user_id=1) is False


async def test_unconfirmed_email_does_not_count(
    db: async_sessionmaker[AsyncSession], resend_on: None
) -> None:
    async with db() as session:
        session.add(UserEmail2FA(user_id=1))  # no confirmed_at
        await session.commit()
        assert await two_factor.available_factors(session=session, user_id=1) == set()
