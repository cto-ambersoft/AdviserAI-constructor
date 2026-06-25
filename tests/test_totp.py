"""TOTP 2FA enrollment service (B1 / P2-T1).

The secret is stored Fernet-encrypted at rest and the enrollment is only *active*
(``is_enabled``) once a valid code has confirmed it. pyotp generates real codes so
the verify path is exercised end-to-end without mocking.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pyotp
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import SecretCipher
from app.models.base import Base
from app.models.user import User
from app.models.user_recovery_code import UserRecoveryCode
from app.models.user_totp import UserTotp
from app.services.totp import TotpService


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'totp.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as s:
            s.add(User(id=1, email="totp@example.com", hashed_password="x", is_active=True))
            await s.commit()
            yield s
    finally:
        await engine.dispose()


def _service() -> tuple[TotpService, SecretCipher]:
    cipher = SecretCipher(Fernet.generate_key().decode("utf-8"))
    return TotpService(cipher=cipher), cipher


async def test_enroll_persists_encrypted_secret_unconfirmed(session: AsyncSession) -> None:
    service, cipher = _service()

    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")

    assert result["provisioning_uri"].startswith("otpauth://totp/")
    assert "Amber" in result["provisioning_uri"]

    row = await session.scalar(select(UserTotp).where(UserTotp.user_id == 1))
    assert row is not None
    assert row.confirmed_at is None  # not active until verified
    assert row.secret_encrypted != result["secret"]  # encrypted at rest
    assert cipher.decrypt(row.secret_encrypted) == result["secret"]  # round-trips
    assert await service.is_enabled(session=session, user_id=1) is False


async def test_verify_with_valid_code_confirms_and_enables(session: AsyncSession) -> None:
    service, _ = _service()
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    code = pyotp.TOTP(result["secret"]).now()

    assert await service.verify(session=session, user_id=1, code=code) is True
    assert await service.is_enabled(session=session, user_id=1) is True


async def test_verify_with_wrong_code_does_not_enable(session: AsyncSession) -> None:
    service, _ = _service()
    await service.enroll(session=session, user_id=1, account_name="totp@example.com")

    assert await service.verify(session=session, user_id=1, code="000000") is False
    assert await service.is_enabled(session=session, user_id=1) is False


async def test_verify_without_enrollment_is_false(session: AsyncSession) -> None:
    service, _ = _service()
    assert await service.verify(session=session, user_id=1, code="123456") is False


async def test_re_enroll_before_confirmation_replaces_secret(session: AsyncSession) -> None:
    service, _ = _service()
    first = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    second = await service.enroll(session=session, user_id=1, account_name="totp@example.com")

    assert first["secret"] != second["secret"]
    # The old secret no longer verifies; only one enrollment row exists.
    old_code = pyotp.TOTP(first["secret"]).now()
    assert await service.verify(session=session, user_id=1, code=old_code) is False
    rows = list(await session.scalars(select(UserTotp).where(UserTotp.user_id == 1)))
    assert len(rows) == 1


async def test_disable_removes_enrollment(session: AsyncSession) -> None:
    service, _ = _service()
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())
    assert await service.is_enabled(session=session, user_id=1) is True

    await service.disable(session=session, user_id=1)

    assert await service.is_enabled(session=session, user_id=1) is False
    assert await session.scalar(select(UserTotp).where(UserTotp.user_id == 1)) is None


# ─────────────────────────── recovery codes (P2-T4) ───────────────────────────


async def _recovery_rows(session: AsyncSession) -> list[UserRecoveryCode]:
    rows = await session.scalars(select(UserRecoveryCode).where(UserRecoveryCode.user_id == 1))
    return list(rows)


async def test_enroll_generates_hashed_recovery_codes(session: AsyncSession) -> None:
    service, _ = _service()
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")

    codes = result["recovery_codes"]
    assert len(codes) == 10
    rows = await _recovery_rows(session)
    assert len(rows) == 10
    assert all(r.code_hash not in codes for r in rows)  # hashed, not plaintext
    assert all(r.used_at is None for r in rows)


async def test_recovery_code_verifies_once_when_enabled(session: AsyncSession) -> None:
    service, _ = _service()
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    # Enable via a real TOTP code first.
    await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())
    recovery = result["recovery_codes"][0]

    # Recovery is a step-up fallback, gated behind allow_recovery=True.
    assert (
        await service.verify(session=session, user_id=1, code=recovery, allow_recovery=True)
        is True
    )
    assert await service.is_enabled(session=session, user_id=1) is True
    # One-time: the same recovery code cannot be reused.
    assert (
        await service.verify(session=session, user_id=1, code=recovery, allow_recovery=True)
        is False
    )


async def test_recovery_code_rejected_before_confirmation(session: AsyncSession) -> None:
    service, _ = _service()
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    # Recovery codes are a fallback for an ENABLED enrollment, not a way to confirm one.
    recovery = result["recovery_codes"][0]
    assert (
        await service.verify(session=session, user_id=1, code=recovery, allow_recovery=True)
        is False
    )
    assert await service.is_enabled(session=session, user_id=1) is False


async def test_verify_without_allow_recovery_ignores_recovery_code(session: AsyncSession) -> None:
    # I2 — the enrollment-confirmation path (allow_recovery=False, the default) must
    # NOT accept or consume a recovery code; it stays available for step-up.
    service, _ = _service()
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())
    recovery = result["recovery_codes"][0]

    assert await service.verify(session=session, user_id=1, code=recovery) is False
    # Not consumed → still usable as a step-up fallback.
    assert (
        await service.verify(session=session, user_id=1, code=recovery, allow_recovery=True)
        is True
    )


async def test_recovery_codes_are_one_time_and_exhaustible(session: AsyncSession) -> None:
    # I6 — every issued recovery code works exactly once; after the set is spent,
    # a reused code no longer verifies.
    service, _ = _service()
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())

    for code in result["recovery_codes"]:
        ok = await service.verify(session=session, user_id=1, code=code, allow_recovery=True)
        assert ok is True
    # All consumed — reusing the first code fails.
    assert (
        await service.verify(
            session=session, user_id=1, code=result["recovery_codes"][0], allow_recovery=True
        )
        is False
    )


async def test_disable_clears_recovery_codes(session: AsyncSession) -> None:
    service, _ = _service()
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())

    await service.disable(session=session, user_id=1)
    assert await _recovery_rows(session) == []


# ───────────────────────── brute-force lockout (C1) ─────────────────────────


def _service_locked(max_attempts: int = 3) -> TotpService:
    return TotpService(
        cipher=SecretCipher(Fernet.generate_key().decode("utf-8")), max_attempts=max_attempts
    )


async def test_lockout_after_repeated_failures(session: AsyncSession) -> None:
    from app.services.totp import TotpLockedError

    service = _service_locked(max_attempts=3)
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())

    for _ in range(3):
        assert await service.verify(session=session, user_id=1, code="000000") is False
    # Now locked — even a correct code is refused until the window passes.
    with pytest.raises(TotpLockedError):
        await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())


async def test_success_resets_failure_counter(session: AsyncSession) -> None:
    service = _service_locked(max_attempts=3)
    result = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    code = pyotp.TOTP(result["secret"])
    await service.verify(session=session, user_id=1, code=code.now())  # confirm + reset

    assert await service.verify(session=session, user_id=1, code="000000") is False
    assert await service.verify(session=session, user_id=1, code="000000") is False
    assert await service.verify(session=session, user_id=1, code=code.now()) is True  # resets
    # Two fresh failures must not lock — the counter was reset by the success above.
    assert await service.verify(session=session, user_id=1, code="000000") is False
    assert await service.verify(session=session, user_id=1, code="000000") is False
    assert await service.verify(session=session, user_id=1, code=code.now()) is True


async def test_re_enroll_regenerates_recovery_codes(session: AsyncSession) -> None:
    service, _ = _service()
    first = await service.enroll(session=session, user_id=1, account_name="totp@example.com")
    second = await service.enroll(session=session, user_id=1, account_name="totp@example.com")

    assert set(first["recovery_codes"]).isdisjoint(second["recovery_codes"])
    assert len(await _recovery_rows(session)) == 10  # only the new set remains
