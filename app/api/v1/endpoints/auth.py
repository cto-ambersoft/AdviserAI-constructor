from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, RequireStepUp
from app.core.auth import (
    create_access_token,
    create_login_challenge_token,
    create_refresh_token,
    create_step_up_token,
    decode_login_challenge_token,
    decode_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.core.config import get_settings
from app.core.ratelimit import check_rate_limit
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import (
    AuthUserResponse,
    Email2FACodeSentResponse,
    Email2FAConfirmRequest,
    Email2FAStatusResponse,
    EmailConfirmRequest,
    EmailConfirmStatusResponse,
    EmailConfirmVerifyRequest,
    EmailConfirmVerifyResponse,
    RefreshTokenRequest,
    SignInRequest,
    SignUpRequest,
    StepUpRequest,
    StepUpResponse,
    TokenResponse,
    TotpEnrollResponse,
    TotpStatusResponse,
    TotpVerifyRequest,
    TwoFactorLoginEmailRequest,
    TwoFactorLoginRequest,
    TwoFactorRequiredResponse,
    UserRead,
)
from app.services import email_2fa, email_confirm, two_factor
from app.services.email_2fa import Email2FALockedError
from app.services.totp import TotpLockedError, TotpService

router = APIRouter()

totp_service = TotpService()


async def _enforce_login_rate_limit(request: Request, *scopes: tuple[str, str]) -> None:
    """Throttle login attempts (T5/S7). ``scopes`` are (label, value) pairs — e.g.
    the source IP and the target email — each counted independently. Raises 429 when
    any scope exceeds the configured window limit.
    """
    settings = get_settings()
    limit = settings.login_rate_limit_max_attempts
    window = settings.login_rate_limit_window_seconds
    for label, value in scopes:
        allowed = await check_rate_limit(
            f"login_rl:{label}:{value}", limit=limit, window_seconds=window
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Please wait and try again.",
                headers={"Retry-After": str(window)},
            )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _locked_response(exc: TotpLockedError | Email2FALockedError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many 2FA attempts. Try again later.",
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


async def _enforce_per_user_rate_limit(user_id: int, scope: str) -> None:
    """Throttle a per-user action (email-2FA code requests / verifies). Reuses the
    login rate-limit window. Raises 429 when exceeded. Fails open on Redis outage
    (the per-factor DB lockout remains the hard control)."""
    settings = get_settings()
    allowed = await check_rate_limit(
        f"{scope}:{user_id}",
        limit=settings.login_rate_limit_max_attempts,
        window_seconds=settings.login_rate_limit_window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait and try again.",
            headers={"Retry-After": str(settings.login_rate_limit_window_seconds)},
        )


def _normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _issue_token_pair(session: DbSession, user: User) -> TokenResponse:
    access_token, expires_in = create_access_token(subject=user.email)
    refresh_token, refresh_expires_in, refresh_expires_at = create_refresh_token(
        subject=str(user.id)
    )
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=refresh_expires_at,
            revoked_at=None,
        )
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        refresh_expires_in=refresh_expires_in,
    )


@router.post("/signup", response_model=AuthUserResponse, status_code=status.HTTP_201_CREATED)
async def sign_up(payload: SignUpRequest, session: DbSession) -> AuthUserResponse:
    existing_user = await session.scalar(select(User).where(User.email == payload.email))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists.",
        )

    user = User(
        email=payload.email, hashed_password=hash_password(payload.password), is_active=True
    )
    session.add(user)
    await session.flush()
    token_response = await _issue_token_pair(session=session, user=user)
    await session.commit()
    await session.refresh(user)
    return AuthUserResponse(
        user=UserRead.model_validate(user),
        token=token_response,
    )


@router.post("/signin", response_model=TokenResponse | TwoFactorRequiredResponse)
async def sign_in(
    payload: SignInRequest, session: DbSession, request: Request
) -> TokenResponse | TwoFactorRequiredResponse:
    await _enforce_login_rate_limit(
        request, ("ip", _client_ip(request)), ("email", payload.email.lower())
    )
    user = await session.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is inactive.",
        )
    # Password OK. If the user has any second factor, withhold tokens and hand back a
    # short-lived challenge to be redeemed (with a code) at /2fa/login. The response
    # advertises the available factors so the UI offers the right path(s). Users
    # WITHOUT 2FA fall straight through to the unchanged token-pair response.
    factors = await two_factor.available_factors(session=session, user_id=user.id)
    if factors:
        challenge_token, expires_in = create_login_challenge_token(subject=user.email)
        return TwoFactorRequiredResponse(
            challenge_token=challenge_token,
            expires_in=expires_in,
            factors=sorted(factors),
        )
    token_response = await _issue_token_pair(session=session, user=user)
    await session.commit()
    return token_response


async def _user_from_login_challenge(session: DbSession, challenge_token: str) -> User:
    """Resolve the user behind a valid login challenge (proves the password already
    passed). Raises 401 on a bad/expired challenge or inactive user."""
    email = decode_login_challenge_token(challenge_token)
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate the 2FA challenge.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


@router.post("/2fa/login/email/request", response_model=Email2FACodeSentResponse)
async def request_login_email_code(
    payload: TwoFactorLoginEmailRequest, session: DbSession, request: Request
) -> Email2FACodeSentResponse:
    """Email an ``email_2fa_login`` code for the user behind a valid login challenge.

    The challenge token is validated first (it proves the password already passed),
    so this can't be used for user enumeration; rate-limited per source IP. To avoid
    enumeration, a user without email-2FA still yields a generic ``sent: True`` (no
    code is actually sent).
    """
    await _enforce_login_rate_limit(request, ("ip", _client_ip(request)))
    user = await _user_from_login_challenge(session, payload.challenge_token)
    if await email_2fa.is_enabled(session=session, user_id=user.id):
        await email_2fa.send_code(
            session=session, user=user, action=email_2fa.ACTION_LOGIN
        )
    return Email2FACodeSentResponse(sent=True)


@router.post("/2fa/login", response_model=TokenResponse)
async def login_2fa(
    payload: TwoFactorLoginRequest, session: DbSession, request: Request
) -> TokenResponse:
    """Second step of login-2FA: exchange the challenge + a second-factor code for the
    token pair. ``method=totp`` (default) reuses TotpService.verify (brute-force
    lockout + recovery-code fallback); ``method=email`` consumes an emailed
    ``email_2fa_login`` code requested via /2fa/login/email/request.
    """
    await _enforce_login_rate_limit(request, ("ip", _client_ip(request)))
    user = await _user_from_login_challenge(session, payload.challenge_token)
    if payload.method == "email":
        if not await email_2fa.is_enabled(session=session, user_id=user.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 2FA code."
            )
        try:
            ok = await email_2fa.verify(
                session=session, user=user, action=email_2fa.ACTION_LOGIN, code=payload.code
            )
        except Email2FALockedError as exc:
            raise _locked_response(exc) from exc
    else:
        try:
            ok = await totp_service.verify(
                session=session, user_id=user.id, code=payload.code, allow_recovery=True
            )
        except TotpLockedError as exc:
            raise _locked_response(exc) from exc
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 2FA code."
        )
    token_response = await _issue_token_pair(session=session, user=user)
    await session.commit()
    return token_response


@router.post("/refresh", response_model=TokenResponse)
async def refresh_access_token(payload: RefreshTokenRequest, session: DbSession) -> TokenResponse:
    subject, _ = decode_refresh_token(payload.refresh_token)
    try:
        user_id = int(subject)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    existing_token = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(payload.refresh_token))
    )
    if existing_token is None or existing_token.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if existing_token.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token already used.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if _normalize_dt(existing_token.expires_at) <= datetime.now(UTC):
        existing_token.revoked_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        existing_token.revoked_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    existing_token.revoked_at = datetime.now(UTC)
    token_response = await _issue_token_pair(session=session, user=user)
    await session.commit()
    return token_response


@router.get("/me", response_model=UserRead)
async def read_me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


# ───────────────────────────── 2FA (TOTP) ──────────────────────────────


@router.post("/2fa/enroll", response_model=TotpEnrollResponse)
async def enroll_2fa(current_user: CurrentUser, session: DbSession) -> TotpEnrollResponse:
    # Block re-enrolling while active: it resets confirmed_at and would silently
    # disable 2FA. Disable first (and, once step-up lands, that itself is gated).
    if await totp_service.is_enabled(session=session, user_id=current_user.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="2FA is already enabled. Disable it before re-enrolling.",
        )
    result = await totp_service.enroll(
        session=session, user_id=current_user.id, account_name=current_user.email
    )
    return TotpEnrollResponse(
        provisioning_uri=result["provisioning_uri"],
        secret=result["secret"],
        recovery_codes=result["recovery_codes"],
    )


@router.post("/2fa/verify", response_model=TotpStatusResponse)
async def verify_2fa(
    payload: TotpVerifyRequest, current_user: CurrentUser, session: DbSession
) -> TotpStatusResponse:
    try:
        ok = await totp_service.verify(
            session=session, user_id=current_user.id, code=payload.code
        )
    except TotpLockedError as exc:
        raise _locked_response(exc) from exc
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 2FA code."
        )
    return TotpStatusResponse(enabled=True)


@router.get("/2fa/status", response_model=TotpStatusResponse)
async def status_2fa(current_user: CurrentUser, session: DbSession) -> TotpStatusResponse:
    enabled = await totp_service.is_enabled(session=session, user_id=current_user.id)
    return TotpStatusResponse(enabled=enabled)


@router.post("/2fa/step-up/email/request", response_model=Email2FACodeSentResponse)
async def request_step_up_email_code(
    current_user: CurrentUser, session: DbSession
) -> Email2FACodeSentResponse:
    """Email an ``email_2fa_step_up`` code for a user with email-2FA enrolled. The
    code is then submitted to /2fa/step-up with ``method=email``. Rate-limited."""
    if not await email_2fa.is_enabled(session=session, user_id=current_user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email-based two-factor authentication is not enabled.",
        )
    await _enforce_per_user_rate_limit(current_user.id, "email_2fa_step_up_request")
    await email_2fa.send_code(
        session=session, user=current_user, action=email_2fa.ACTION_STEP_UP
    )
    return Email2FACodeSentResponse(sent=True)


@router.post("/2fa/step-up", response_model=StepUpResponse)
async def step_up_2fa(
    payload: StepUpRequest, current_user: CurrentUser, session: DbSession
) -> StepUpResponse:
    """Exchange a fresh second-factor code for a short-lived step-up token used to
    authorize critical actions. Accepts either a TOTP/recovery code (``method=totp``,
    default) or an emailed code (``method=email``). The minted token is
    factor-agnostic, so ``require_step_up`` and the gated-endpoint list are unchanged.
    """
    if payload.method == "email":
        if not await email_2fa.is_enabled(session=session, user_id=current_user.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email-based two-factor authentication is not enabled.",
            )
        try:
            ok = await email_2fa.verify(
                session=session,
                user=current_user,
                action=email_2fa.ACTION_STEP_UP,
                code=payload.code,
            )
        except Email2FALockedError as exc:
            raise _locked_response(exc) from exc
    else:
        if not await totp_service.is_enabled(session=session, user_id=current_user.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="2FA is not enabled."
            )
        # Step-up is the re-auth surface, so a one-time recovery code is an accepted
        # fallback here (unlike /2fa/verify, which is TOTP-only).
        try:
            ok = await totp_service.verify(
                session=session,
                user_id=current_user.id,
                code=payload.code,
                allow_recovery=True,
            )
        except TotpLockedError as exc:
            raise _locked_response(exc) from exc
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 2FA code."
        )
    token, expires_in = create_step_up_token(subject=current_user.email)
    return StepUpResponse(step_up_token=token, expires_in=expires_in)


@router.delete("/2fa", response_model=TotpStatusResponse)
async def disable_2fa(current_user: RequireStepUp, session: DbSession) -> TotpStatusResponse:
    # Disabling 2FA is itself a critical action — gated by step-up so a hijacked
    # session can't strip the second factor.
    await totp_service.disable(session=session, user_id=current_user.id)
    return TotpStatusResponse(enabled=False)


# ──────────────────────── 2FA (email as a full factor) ─────────────────────────


@router.post("/2fa/email/enroll", response_model=Email2FACodeSentResponse)
async def enroll_email_2fa(
    current_user: CurrentUser, session: DbSession
) -> Email2FACodeSentResponse:
    """Begin email-2FA enrollment: email a code the user must confirm (verify-on-
    enroll — the account email is not implicitly trusted). Requires Resend to be
    configured (503 otherwise). Throttled per user to prevent email-bombing.
    """
    if not email_2fa.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email-based two-factor authentication is not configured.",
        )
    await _enforce_per_user_rate_limit(current_user.id, "email_2fa_enroll")
    await email_2fa.send_enrollment_code(session=session, user=current_user)
    return Email2FACodeSentResponse(sent=True)


@router.post("/2fa/email/confirm", response_model=Email2FAStatusResponse)
async def confirm_email_2fa(
    payload: Email2FAConfirmRequest, current_user: CurrentUser, session: DbSession
) -> Email2FAStatusResponse:
    """Confirm enrollment with the emailed code → activates email-2FA. Wrong/expired
    code → 400; per-factor lockout → 429."""
    await _enforce_per_user_rate_limit(current_user.id, "email_2fa_confirm")
    try:
        ok = await email_2fa.verify(
            session=session,
            user=current_user,
            action=email_2fa.ACTION_ENROLL,
            code=payload.code,
            confirm_enrollment=True,
        )
    except Email2FALockedError as exc:
        raise _locked_response(exc) from exc
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code."
        )
    return Email2FAStatusResponse(enabled=True, available=True)


@router.get("/2fa/email/status", response_model=Email2FAStatusResponse)
async def status_email_2fa(
    current_user: CurrentUser, session: DbSession
) -> Email2FAStatusResponse:
    enabled = await email_2fa.is_enabled(session=session, user_id=current_user.id)
    return Email2FAStatusResponse(enabled=enabled, available=email_2fa.is_configured())


@router.delete("/2fa/email", response_model=Email2FAStatusResponse)
async def disable_email_2fa(
    current_user: RequireStepUp, session: DbSession
) -> Email2FAStatusResponse:
    # Disabling a factor is itself critical — step-up gated so a hijacked session
    # can't strip it. Disabling the last factor is allowed (returns to "no 2FA").
    await email_2fa.disable(session=session, user_id=current_user.id)
    return Email2FAStatusResponse(enabled=False, available=email_2fa.is_configured())


@router.post("/email-confirm/request", response_model=EmailConfirmStatusResponse)
async def request_email_confirmation(
    payload: EmailConfirmRequest,
    current_user: CurrentUser,
    session: DbSession,
    request: Request,
) -> EmailConfirmStatusResponse:
    """T20 (W11c): email a one-time confirmation code for a critical action.

    Disabled (503) when Resend isn't configured — existing flows are unaffected.
    Throttled per user to prevent email-bombing.
    """
    if not email_confirm.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email confirmation is not configured.",
        )
    settings = get_settings()
    allowed = await check_rate_limit(
        f"email_confirm:{current_user.id}",
        limit=settings.login_rate_limit_max_attempts,
        window_seconds=settings.login_rate_limit_window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many confirmation requests. Please wait and try again.",
            headers={"Retry-After": str(settings.login_rate_limit_window_seconds)},
        )
    await email_confirm.request_confirmation(
        session=session, user=current_user, action=payload.action
    )
    return EmailConfirmStatusResponse(sent=True, enabled=True)


@router.post("/email-confirm/verify", response_model=EmailConfirmVerifyResponse)
async def verify_email_confirmation(
    payload: EmailConfirmVerifyRequest,
    current_user: CurrentUser,
    session: DbSession,
) -> EmailConfirmVerifyResponse:
    # C1: throttle verify per (user, action) so an emailed code can't be
    # brute-forced within its TTL (defence-in-depth with the high-entropy code).
    settings = get_settings()
    allowed = await check_rate_limit(
        f"email_confirm_verify:{current_user.id}:{payload.action}",
        limit=settings.login_rate_limit_max_attempts,
        window_seconds=settings.login_rate_limit_window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many confirmation attempts. Please wait and try again.",
            headers={"Retry-After": str(settings.login_rate_limit_window_seconds)},
        )
    confirmed = await email_confirm.verify_confirmation(
        session=session, user=current_user, action=payload.action, code=payload.code
    )
    return EmailConfirmVerifyResponse(confirmed=confirmed)
