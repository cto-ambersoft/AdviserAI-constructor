"""T20 (W11c): email confirmation for critical actions via Resend.

Codes are stored hashed, single-use, TTL-bounded. The feature is gated on
RESEND_API_KEY + EMAIL_FROM (empty → disabled). The Resend HTTP call is mocked.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.email_confirm as ec
from app.core.config import get_settings
from app.models.base import Base
from app.models.user import User
from app.models.user_email_confirmation import UserEmailConfirmation


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'email.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _enable(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    monkeypatch.setattr(get_settings(), "email_from", "no-reply@ambersoft.llc")
    sent: list[dict[str, object]] = []

    async def _fake_send(**kw: object) -> None:
        sent.append(kw)

    monkeypatch.setattr(ec, "_send_resend_email", _fake_send)
    return sent


async def _user(session: AsyncSession) -> User:
    user = User(email="u@x.io", hashed_password="x", is_active=True)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def test_disabled_without_key() -> None:
    # Default settings have no Resend key → feature off, existing flows unchanged.
    assert ec.is_enabled() is False


async def test_request_disabled_raises(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "resend_api_key", "")
    async with db() as session:
        user = await _user(session)
        with pytest.raises(ec.EmailConfirmationNotConfigured):
            await ec.request_confirmation(session=session, user=user, action="x")


async def test_request_stores_hashed_code_and_sends(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _enable(monkeypatch)
    monkeypatch.setattr(ec, "_generate_code", lambda: "123456")
    async with db() as session:
        user = await _user(session)
        await ec.request_confirmation(
            session=session, user=user, action="change_exchange_key"
        )
        rows = list(await session.scalars(select(UserEmailConfirmation)))
        assert len(rows) == 1
        assert rows[0].code_hash not in ("", "123456")  # hashed, not plaintext
        assert rows[0].consumed_at is None
        assert len(sent) == 1 and sent[0]["to"] == "u@x.io"


async def test_verify_happy_path_is_single_use(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(ec, "_generate_code", lambda: "123456")
    async with db() as session:
        user = await _user(session)
        await ec.request_confirmation(session=session, user=user, action="act")
        assert (
            await ec.verify_confirmation(
                session=session, user=user, action="act", code="123456"
            )
            is True
        )
        # reuse rejected
        assert (
            await ec.verify_confirmation(
                session=session, user=user, action="act", code="123456"
            )
            is False
        )


async def test_verify_rejects_wrong_code_and_wrong_action(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(ec, "_generate_code", lambda: "123456")
    async with db() as session:
        user = await _user(session)
        await ec.request_confirmation(session=session, user=user, action="act")
        assert (
            await ec.verify_confirmation(
                session=session, user=user, action="act", code="000000"
            )
            is False
        )
        assert (
            await ec.verify_confirmation(
                session=session, user=user, action="other", code="123456"
            )
            is False
        )


async def test_requesting_again_invalidates_the_prior_code(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # C1: only the latest code is valid — re-request invalidates the previous one.
    _enable(monkeypatch)
    codes = iter(["first0", "second"])
    monkeypatch.setattr(ec, "_generate_code", lambda: next(codes))
    async with db() as session:
        user = await _user(session)
        await ec.request_confirmation(session=session, user=user, action="act")
        await ec.request_confirmation(session=session, user=user, action="act")
        assert (
            await ec.verify_confirmation(
                session=session, user=user, action="act", code="first0"
            )
            is False
        )
        assert (
            await ec.verify_confirmation(
                session=session, user=user, action="act", code="second"
            )
            is True
        )


def test_generated_code_is_high_entropy() -> None:
    # C1: not a 6-digit numeric — high-entropy token.
    code = ec._generate_code()
    assert len(code) >= 10
    assert not code.isdigit()


async def test_verify_rejects_expired_code(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(ec, "_generate_code", lambda: "123456")
    monkeypatch.setattr(get_settings(), "email_confirm_code_ttl_minutes", -1)  # already expired
    async with db() as session:
        user = await _user(session)
        await ec.request_confirmation(session=session, user=user, action="act")
        assert (
            await ec.verify_confirmation(
                session=session, user=user, action="act", code="123456"
            )
            is False
        )
