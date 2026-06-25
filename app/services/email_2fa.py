"""Email-2FA enrollment service — email as a full, opt-in second factor.

Mirrors :mod:`app.services.totp`, but there is no shared secret: codes are
delivered and verified through the existing :mod:`app.services.email_confirm`
service (Resend send + hashed, single-use, TTL-bounded codes). This module owns the
*enrollment* concept — one :class:`UserEmail2FA` row per user, active only once the
user proves control of the account email (``confirmed_at``) — plus the per-factor
brute-force lockout, exactly like TOTP.

Three reserved ``email_confirm`` actions keep a code minted for one purpose from
being replayed for another:

- ``email_2fa_enroll``   — activate the factor (verify-on-enroll)
- ``email_2fa_login``    — second factor at sign-in
- ``email_2fa_step_up``  — re-auth a critical action

The whole factor is gated on Resend being configured (``email_confirm.is_enabled``):
with no Resend key it can neither be enrolled nor advertised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.timeutils import as_aware_utc
from app.models.user import User
from app.models.user_email_2fa import UserEmail2FA
from app.services import email_confirm

# Reserved actions (see module docstring). Distinct so a code is single-purpose.
ACTION_ENROLL = "email_2fa_enroll"
ACTION_LOGIN = "email_2fa_login"
ACTION_STEP_UP = "email_2fa_step_up"


class Email2FALockedError(Exception):
    """Raised when email-2FA verification is temporarily locked after too many fails."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Email 2FA verification is temporarily locked.")
        self.retry_after_seconds = retry_after_seconds


def is_configured() -> bool:
    """True when Resend is configured, so email-2FA can be used at all."""
    return email_confirm.is_enabled()


async def _get(session: AsyncSession, user_id: int) -> UserEmail2FA | None:
    row: UserEmail2FA | None = await session.scalar(
        select(UserEmail2FA).where(UserEmail2FA.user_id == user_id)
    )
    return row


async def is_enabled(*, session: AsyncSession, user_id: int) -> bool:
    """True only when the user has a *confirmed* email-2FA enrollment."""
    row = await _get(session, user_id)
    return row is not None and row.confirmed_at is not None


async def send_enrollment_code(*, session: AsyncSession, user: User) -> None:
    """Create or refresh an unconfirmed enrollment and email an enroll code.

    Re-enrolling before confirmation simply re-sends a code against the same pending
    row (``email_confirm`` invalidates any prior code). Raises
    :class:`email_confirm.EmailConfirmationNotConfigured` when Resend is off.
    """
    row = await _get(session, user.id)
    if row is None:
        row = UserEmail2FA(user_id=user.id)
        session.add(row)
        await session.flush()
    # request_confirmation commits the code row; the pending enrollment row flushes
    # in the same session and is committed alongside it.
    await email_confirm.request_confirmation(session=session, user=user, action=ACTION_ENROLL)


async def send_code(*, session: AsyncSession, user: User, action: str) -> None:
    """Email a code for ``action`` (login/step-up). Caller guarantees the factor is
    confirmed. Raises if Resend is off."""
    await email_confirm.request_confirmation(session=session, user=user, action=action)


async def verify(
    *,
    session: AsyncSession,
    user: User,
    action: str,
    code: str,
    confirm_enrollment: bool = False,
) -> bool:
    """Verify an emailed code for ``action`` under the per-factor lockout.

    Delegates the actual code check (hash match, single-use, TTL) to
    ``email_confirm.verify_confirmation``. On the first successful
    ``confirm_enrollment`` verification, stamps ``confirmed_at`` — that flips the
    factor on. Returns ``False`` when no enrollment row exists. Raises
    :class:`Email2FALockedError` while locked.
    """
    row = await _get(session, user.id)
    if row is None:
        return False

    settings = get_settings()
    now = datetime.now(UTC)
    locked_until = as_aware_utc(row.locked_until)
    if locked_until is not None and locked_until > now:
        raise Email2FALockedError(int((locked_until - now).total_seconds()))

    ok = await email_confirm.verify_confirmation(
        session=session, user=user, action=action, code=code
    )
    if ok and confirm_enrollment and row.confirmed_at is None:
        row.confirmed_at = now

    if ok:
        row.failed_attempts = 0
        row.locked_until = None
    else:
        row.failed_attempts += 1
        if row.failed_attempts >= settings.totp_max_failed_attempts:
            # Latch the cooldown and reset the counter so the next window is fresh.
            row.locked_until = now + timedelta(minutes=settings.totp_lockout_minutes)
            row.failed_attempts = 0
    await session.commit()
    return ok


async def disable(*, session: AsyncSession, user_id: int) -> None:
    """Remove the enrollment entirely (idempotent)."""
    row = await _get(session, user_id)
    if row is not None:
        await session.delete(row)
        await session.commit()
