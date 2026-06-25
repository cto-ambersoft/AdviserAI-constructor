"""Unified second-factor checks — the single source of truth for "does this user
have any second factor, and which".

Email-2FA makes email a co-equal factor alongside TOTP. Rather than scatter
``totp_service.is_enabled`` checks (which would silently ignore email-2FA), every
gate asks here: login (``/signin``), step-up (``require_step_up``), and the UI's
factor picker. This is the linchpin of the feature — get it right once.

The ``email`` factor only counts when Resend is configured
(``email_2fa.is_configured``): an env that can't deliver a code must not advertise
or require email-2FA.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import email_2fa
from app.services.totp import TotpService

# Canonical factor identifiers used across the API/UI contract.
FACTOR_TOTP = "totp"
FACTOR_EMAIL = "email"

_totp_service = TotpService()


async def available_factors(*, session: AsyncSession, user_id: int) -> set[str]:
    """The set of confirmed second factors the user can use right now ⊆ {totp,email}.

    ``email`` is included only when the enrollment is confirmed AND Resend is
    configured — a confirmed enrollment in an env that lost its Resend key is not a
    usable factor.
    """
    factors: set[str] = set()
    if await _totp_service.is_enabled(session=session, user_id=user_id):
        factors.add(FACTOR_TOTP)
    if email_2fa.is_configured() and await email_2fa.is_enabled(
        session=session, user_id=user_id
    ):
        factors.add(FACTOR_EMAIL)
    return factors


async def has_second_factor(*, session: AsyncSession, user_id: int) -> bool:
    """True when the user has at least one usable second factor (TOTP or email)."""
    return bool(await available_factors(session=session, user_id=user_id))
