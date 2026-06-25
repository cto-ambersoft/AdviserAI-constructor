import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    bearer_scheme,
    decode_access_token,
    decode_step_up_token,
    get_bearer_token,
)
from app.core.config import get_settings
from app.db.session import get_db_session
from app.models.user import User
from app.services import two_factor
from app.services.totp import TotpService
from app.worker.broker import broker

logger = logging.getLogger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_db_session)]

totp_service = TotpService()


def _get_redis_client() -> Redis:
    return Redis(connection_pool=broker.connection_pool)


async def _consume_step_up_jti(jti: str) -> bool:
    """Single-use enforcement for step-up tokens (I4): one re-auth authorizes one
    action. Returns ``True`` if this jti was unused (and marks it used) via Redis
    SETNX with TTL = the token's max lifetime. Fail-CLOSED on Redis error (T5/S8):
    if we cannot record the jti as used, the token could be replayed within its TTL,
    so the critical action is denied rather than allowing a one-time token to be reused.
    """
    ttl = get_settings().jwt_step_up_token_expire_minutes * 60
    try:
        async with _get_redis_client() as redis:
            was_unused = await redis.set(f"step_up:used:{jti}", "1", nx=True, ex=ttl)
            return bool(was_unused)
    except Exception:
        logger.warning("step-up jti single-use check unavailable (Redis); DENYING (fail-closed)")
        return False


async def get_current_user(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    token = get_bearer_token(credentials)
    email = decode_access_token(token)
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_admin_user(current_user: CurrentUser) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return current_user


CurrentAdminUser = Annotated[User, Depends(get_current_admin_user)]


async def require_step_up(
    current_user: CurrentUser,
    session: DbSession,
    step_up_token: Annotated[str | None, Header(alias="X-Step-Up-Token")] = None,
) -> User:
    """Gate a critical action behind a fresh 2FA step-up.

    Users with *any* second factor (TOTP or email-2FA) must present a valid step-up
    token. Users WITHOUT 2FA: by default pass through (2FA is opt-in) — but when
    ``step_up_require_2fa`` is on (review I8), they are refused so a hijacked no-2FA
    session can't perform critical actions; they must enroll a factor first. The
    step-up token is factor-agnostic, so the rest of this gate is unchanged. Returns
    the user so it drops in for the ``CurrentUser`` dependency on any sensitive
    endpoint.
    """
    if not await two_factor.has_second_factor(session=session, user_id=current_user.id):
        if get_settings().step_up_require_2fa:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This action requires two-factor authentication. Enable 2FA first.",
            )
        return current_user
    if not step_up_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A valid step-up (2FA) authorization is required for this action.",
        )
    subject, jti = decode_step_up_token(step_up_token)
    if subject != current_user.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Step-up authorization does not match the current user.",
        )
    if not await _consume_step_up_jti(jti):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This step-up authorization has already been used.",
        )
    return current_user


RequireStepUp = Annotated[User, Depends(require_step_up)]
