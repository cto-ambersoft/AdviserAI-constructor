"""Email confirmation for critical actions via Resend (T20 / W11c).

A one-time code is emailed and verified to authorize a critical action — a second
factor alongside step-up. The whole feature is gated on ``RESEND_API_KEY`` +
``EMAIL_FROM``: empty → disabled (endpoints report "not configured"), so accounts
that never use it are unaffected. Codes are stored hashed, single-use, TTL-bounded.

Resend HTTP API (context7-verified): ``POST {base}/emails`` with
``Authorization: Bearer <key>`` and an ``Idempotency-Key`` header; JSON body
``{from, to, subject, html}``.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import hash_token
from app.core.config import get_settings
from app.models.user import User
from app.models.user_email_confirmation import UserEmailConfirmation


class EmailConfirmationNotConfigured(RuntimeError):
    """Raised when a confirmation is requested but Resend isn't configured."""


def is_enabled() -> bool:
    settings = get_settings()
    return bool(settings.resend_api_key and settings.email_from)


def _generate_code() -> str:
    # High-entropy URL-safe token (~64 bits) — review C1: a 6-digit numeric code is
    # brute-forceable within the TTL. Still copy-paste-friendly from the email.
    return secrets.token_urlsafe(8)


async def _send_resend_email(
    *, to: str, subject: str, html: str, idempotency_key: str
) -> None:
    """Send one email via the Resend HTTP API. Raises on a non-2xx response."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.email_http_timeout_seconds) as client:
        response = await client.post(
            f"{settings.resend_base_url.rstrip('/')}/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Idempotency-Key": idempotency_key,
            },
            json={"from": settings.email_from, "to": [to], "subject": subject, "html": html},
        )
        response.raise_for_status()


async def request_confirmation(*, session: AsyncSession, user: User, action: str) -> None:
    """Generate + store a one-time code and email it for ``action``."""
    if not is_enabled():
        raise EmailConfirmationNotConfigured("Email confirmation is not configured.")
    settings = get_settings()
    ttl = settings.email_confirm_code_ttl_minutes
    now = datetime.now(UTC)
    # C1: invalidate any outstanding code for this (user, action) so only the latest
    # is ever valid — shrinks the brute-force surface to a single live code.
    await session.execute(
        update(UserEmailConfirmation)
        .where(
            UserEmailConfirmation.user_id == user.id,
            UserEmailConfirmation.action == action,
            UserEmailConfirmation.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    code = _generate_code()
    session.add(
        UserEmailConfirmation(
            user_id=user.id,
            action=action,
            code_hash=hash_token(code),
            expires_at=now + timedelta(minutes=ttl),
            consumed_at=None,
        )
    )
    await session.commit()
    await _send_resend_email(
        to=user.email,
        subject="Your Amber confirmation code",
        html=(
            f"<p>Your confirmation code for <strong>{action}</strong> is "
            f"<strong>{code}</strong>.</p><p>It expires in {ttl} minutes. If you "
            "did not request this, ignore this email.</p>"
        ),
        idempotency_key=str(uuid.uuid4()),
    )


async def verify_confirmation(
    *, session: AsyncSession, user: User, action: str, code: str
) -> bool:
    """Consume the matching unexpired, unused code for ``action``. True on success."""
    row = (
        await session.scalars(
            select(UserEmailConfirmation)
            .where(
                UserEmailConfirmation.user_id == user.id,
                UserEmailConfirmation.action == action,
                UserEmailConfirmation.code_hash == hash_token(code),
                UserEmailConfirmation.consumed_at.is_(None),
            )
            .order_by(UserEmailConfirmation.id.desc())
        )
    ).first()
    if row is None:
        return False
    now = datetime.now(UTC)
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < now:
        return False
    row.consumed_at = now
    await session.commit()
    return True
