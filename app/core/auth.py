from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError
from pwdlib import PasswordHash

from app.core.config import get_settings

pwd_hasher = PasswordHash.recommended()
# HTTP Bearer makes Swagger "Authorize" accept a raw JWT token directly.
bearer_scheme = HTTPBearer(auto_error=False)
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def hash_password(password: str) -> str:
    return pwd_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return pwd_hasher.verify(password, hashed_password)


def create_access_token(subject: str, expires_delta: timedelta | None = None) -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    token = jwt.encode(
        {"sub": subject, "exp": expires_at, "type": ACCESS_TOKEN_TYPE},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    expires_in = int((expires_at - now).total_seconds())
    return token, expires_in


def decode_access_token(token: str) -> str:
    settings = get_settings()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        token_type = payload.get("type")
        if token_type not in (None, ACCESS_TOKEN_TYPE):
            raise credentials_exception
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise credentials_exception
    except ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except JWTError as exc:
        raise credentials_exception from exc
    return subject


def hash_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def create_refresh_token(
    subject: str, expires_delta: timedelta | None = None
) -> tuple[str, int, datetime]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(days=settings.jwt_refresh_token_expire_days)
    )
    token = jwt.encode(
        {
            "sub": subject,
            "exp": expires_at,
            "type": REFRESH_TOKEN_TYPE,
            "jti": token_urlsafe(24),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    expires_in = int((expires_at - now).total_seconds())
    return token, expires_in, expires_at


def decode_refresh_token(token: str) -> tuple[str, str]:
    settings = get_settings()
    unauthorized_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid refresh token.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        token_type = payload.get("type")
        if token_type != REFRESH_TOKEN_TYPE:
            raise unauthorized_exception
        subject = payload.get("sub")
        jti = payload.get("jti")
        if not isinstance(subject, str) or not subject or not isinstance(jti, str) or not jti:
            raise unauthorized_exception
    except ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except JWTError as exc:
        raise unauthorized_exception from exc
    return subject, jti


def get_bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
