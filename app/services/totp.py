"""TOTP (RFC 6238) two-factor enrollment service.

Wraps ``pyotp`` and persists one :class:`UserTotp` row per user. The shared secret
is Fernet-encrypted at rest via :class:`SecretCipher` (the same API Vault primitive
used for exchange keys) and decrypted only in-process to verify a code. A fresh
enrollment is *pending* until a valid code confirms it (``confirmed_at``), so 2FA is
never considered enabled on a half-finished enrollment.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict, cast

import pyotp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import hash_token
from app.core.config import get_settings
from app.core.security import SecretCipher
from app.core.timeutils import as_aware_utc
from app.models.user_recovery_code import UserRecoveryCode
from app.models.user_totp import UserTotp


class TotpLockedError(Exception):
    """Raised when 2FA verification is temporarily locked after too many failures."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("2FA verification is temporarily locked.")
        self.retry_after_seconds = retry_after_seconds

# Shown as the account issuer in the authenticator app (the "Amber" in
# ``otpauth://totp/Amber:<email>``).
TOTP_ISSUER = "Amber"
# One-time recovery codes issued per enrollment. 8 bytes → 16 hex chars (64 bits) —
# ample headroom against a leaked-hash dump while staying human-typeable.
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_BYTES = 8


class EnrollResult(TypedDict):
    secret: str
    provisioning_uri: str
    recovery_codes: list[str]


class TotpService:
    def __init__(
        self,
        cipher: SecretCipher | None = None,
        *,
        max_attempts: int | None = None,
        lockout_minutes: int | None = None,
    ) -> None:
        settings = get_settings()
        self._cipher = cipher or SecretCipher(
            settings.encryption_key, legacy_keys=settings.encryption_legacy_keys
        )
        self._max_attempts = max_attempts or settings.totp_max_failed_attempts
        self._lockout_minutes = lockout_minutes or settings.totp_lockout_minutes

    async def enroll(
        self, *, session: AsyncSession, user_id: int, account_name: str
    ) -> EnrollResult:
        """Generate a fresh secret, persist it encrypted (unconfirmed), and return the
        provisioning URI for a QR code. Re-enrolling before confirmation replaces the
        pending secret (and resets ``confirmed_at``), so a stalled enrollment can be
        restarted cleanly. The plaintext secret is returned ONCE for manual entry.
        """
        secret = pyotp.random_base32()
        provisioning_uri = pyotp.TOTP(secret).provisioning_uri(
            name=account_name, issuer_name=TOTP_ISSUER
        )
        row = await self._get(session, user_id)
        if row is None:
            row = UserTotp(user_id=user_id)
            session.add(row)
        row.secret_encrypted = self._cipher.encrypt(secret)
        row.confirmed_at = None
        recovery_codes = await self._regenerate_recovery_codes(session, user_id)
        await session.commit()
        return EnrollResult(
            secret=secret,
            provisioning_uri=provisioning_uri,
            recovery_codes=recovery_codes,
        )

    async def verify(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        code: str,
        allow_recovery: bool = False,
    ) -> bool:
        """Validate a code against the user's secret (±1 step, ~30s, constant-time).

        On the first successful verification of a pending enrollment, stamp
        ``confirmed_at`` — that is what flips 2FA on. ``allow_recovery`` enables the
        one-time recovery-code fallback, but ONLY for the step-up path — the
        enrollment-confirmation path (``/2fa/verify``) passes ``False`` so a routine
        confirm/status call can never silently burn a recovery code. A recovery code
        can never *confirm* an enrollment (gated on ``confirmed_at``). Returns
        ``False`` when no enrollment exists.
        """
        row = await self._get(session, user_id)
        if row is None:
            return False

        now = datetime.now(UTC)
        locked_until = as_aware_utc(row.locked_until)
        if locked_until is not None and locked_until > now:
            raise TotpLockedError(int((locked_until - now).total_seconds()))

        secret = self._cipher.decrypt(row.secret_encrypted)
        ok = pyotp.TOTP(secret).verify(code, valid_window=1)
        if ok and row.confirmed_at is None:
            row.confirmed_at = now
        if not ok and allow_recovery and row.confirmed_at is not None:
            ok = await self._consume_recovery_code(session, user_id, code)

        if ok:
            row.failed_attempts = 0
            row.locked_until = None
        else:
            row.failed_attempts += 1
            if row.failed_attempts >= self._max_attempts:
                # Latch the cooldown and reset the counter so the next window is fresh.
                row.locked_until = now + timedelta(minutes=self._lockout_minutes)
                row.failed_attempts = 0
        await session.commit()
        return ok

    async def is_enabled(self, *, session: AsyncSession, user_id: int) -> bool:
        """True only when the user has a *confirmed* enrollment."""
        row = await self._get(session, user_id)
        return row is not None and row.confirmed_at is not None

    async def disable(self, *, session: AsyncSession, user_id: int) -> None:
        """Remove the enrollment and its recovery codes entirely (idempotent)."""
        row = await self._get(session, user_id)
        if row is not None:
            await session.delete(row)
        await self._delete_recovery_codes(session, user_id)
        await session.commit()

    async def _get(self, session: AsyncSession, user_id: int) -> UserTotp | None:
        row: UserTotp | None = await session.scalar(
            select(UserTotp).where(UserTotp.user_id == user_id)
        )
        return row

    async def _delete_recovery_codes(self, session: AsyncSession, user_id: int) -> None:
        rows = await session.scalars(
            select(UserRecoveryCode).where(UserRecoveryCode.user_id == user_id)
        )
        for row in rows:
            await session.delete(row)

    async def _regenerate_recovery_codes(self, session: AsyncSession, user_id: int) -> list[str]:
        """Replace any existing codes with a fresh set; return the plaintext (once)."""
        await self._delete_recovery_codes(session, user_id)
        await session.flush()  # apply deletes before inserts (avoid hash UQ clashes)
        codes = [secrets.token_hex(RECOVERY_CODE_BYTES) for _ in range(RECOVERY_CODE_COUNT)]
        for code in codes:
            session.add(UserRecoveryCode(user_id=user_id, code_hash=hash_token(code)))
        return codes

    async def _consume_recovery_code(
        self, session: AsyncSession, user_id: int, code: str
    ) -> bool:
        # Atomic compare-and-set (I3): a single conditional UPDATE on the unused row
        # so two concurrent requests with the same code can't both succeed (the row
        # lock serializes them; the second sees used_at already set → rowcount 0).
        result = await session.execute(
            update(UserRecoveryCode)
            .where(
                UserRecoveryCode.user_id == user_id,
                UserRecoveryCode.code_hash == hash_token(code),
                UserRecoveryCode.used_at.is_(None),
            )
            .values(used_at=datetime.now(UTC))
        )
        # The caller (``verify``) owns the single commit (S5); the conditional UPDATE
        # is already atomic at execute time, so the rowcount is authoritative here.
        return int(cast(Any, result).rowcount or 0) == 1
