from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.auth import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import (
    AuthUserResponse,
    RefreshTokenRequest,
    SignInRequest,
    SignUpRequest,
    TokenResponse,
    UserRead,
)

router = APIRouter()


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


@router.post("/signin", response_model=TokenResponse)
async def sign_in(payload: SignInRequest, session: DbSession) -> TokenResponse:
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
