"""Login-2FA: a post-password TOTP challenge for users who have 2FA enabled.

Non-2FA users must keep signing in exactly as before (unchanged TokenResponse) —
the first test pins that. 2FA users get a challenge token from /signin and exchange
it (+ a TOTP/recovery code) at /2fa/login for the real token pair.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.core.auth import hash_password
from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base
from app.models.user import User
from app.services.totp import TotpService

_PASSWORD = "correct-horse-battery"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'login2fa.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db(db: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    async def _get_test_db() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db
    # signin/2fa-login are unauthenticated; make sure no stale current_user override leaks in.
    app.dependency_overrides.pop(get_current_user, None)
    yield
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture(autouse=True)
def _login_rate_limit_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # T5's login rate-limiter uses real Redis; force fail-open so these tests are
    # deterministic regardless of whether Redis is reachable in the environment.
    # The dedicated rate-limit test re-patches _get_redis_client with its own fake.
    import app.core.ratelimit as rl

    def _down() -> object:
        raise ConnectionError("redis disabled in tests")

    monkeypatch.setattr(rl, "_get_redis_client", _down)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_user(
    db: async_sessionmaker[AsyncSession], *, email: str, with_2fa: bool
) -> str | None:
    """Seed a user (id=1). When with_2fa, enroll+confirm and return the TOTP secret."""
    async with db() as session:
        session.add(
            User(id=1, email=email, hashed_password=hash_password(_PASSWORD), is_active=True)
        )
        await session.commit()
        if not with_2fa:
            return None
        # Default cipher (get_settings().encryption_key) so the secret round-trips
        # with the endpoint's module-level TotpService().
        service = TotpService()
        result = await service.enroll(session=session, user_id=1, account_name=email)
        await service.verify(session=session, user_id=1, code=pyotp.TOTP(result["secret"]).now())
        return result["secret"]


class _CountingRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    async def __aenter__(self) -> "_CountingRedis":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> bool:
        return True


async def test_signin_is_rate_limited_per_ip(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # T5 (S7): repeated /signin attempts from one source are throttled (429).
    await _seed_user(db, email="rl@x.io", with_2fa=False)
    import app.core.ratelimit as rl

    shared = _CountingRedis()
    monkeypatch.setattr(rl, "_get_redis_client", lambda: shared)
    monkeypatch.setattr(get_settings(), "login_rate_limit_max_attempts", 2)
    monkeypatch.setattr(get_settings(), "login_rate_limit_window_seconds", 60)

    async with _client() as http:
        first = await http.post(
            "/api/v1/auth/signin", json={"email": "rl@x.io", "password": _PASSWORD}
        )
        second = await http.post(
            "/api/v1/auth/signin", json={"email": "rl@x.io", "password": _PASSWORD}
        )
        blocked = await http.post(
            "/api/v1/auth/signin", json={"email": "rl@x.io", "password": _PASSWORD}
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert blocked.status_code == 429, blocked.text
    assert "retry-after" in {k.lower() for k in blocked.headers}


async def test_signin_without_2fa_returns_tokens_unchanged(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Regression guard: a user WITHOUT 2FA logs in exactly as before.
    await _seed_user(db, email="nofa@x.io", with_2fa=False)
    async with _client() as http:
        resp = await http.post(
            "/api/v1/auth/signin", json={"email": "nofa@x.io", "password": _PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body["access_token"], str)
        assert isinstance(body["refresh_token"], str)
        assert "two_factor_required" not in body


async def test_signin_with_2fa_returns_challenge_not_tokens(
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(db, email="hasfa@x.io", with_2fa=True)
    async with _client() as http:
        resp = await http.post(
            "/api/v1/auth/signin", json={"email": "hasfa@x.io", "password": _PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["two_factor_required"] is True
        assert isinstance(body["challenge_token"], str)
        assert "access_token" not in body  # no tokens issued until the code is verified


async def test_2fa_login_with_valid_code_returns_tokens(
    db: async_sessionmaker[AsyncSession],
) -> None:
    secret = await _seed_user(db, email="hasfa@x.io", with_2fa=True)
    async with _client() as http:
        challenge = (
            await http.post(
                "/api/v1/auth/signin", json={"email": "hasfa@x.io", "password": _PASSWORD}
            )
        ).json()["challenge_token"]

        resp = await http.post(
            "/api/v1/auth/2fa/login",
            json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()},
        )
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json()["access_token"], str)


async def test_2fa_login_with_wrong_code_is_rejected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(db, email="hasfa@x.io", with_2fa=True)
    async with _client() as http:
        challenge = (
            await http.post(
                "/api/v1/auth/signin", json={"email": "hasfa@x.io", "password": _PASSWORD}
            )
        ).json()["challenge_token"]
        resp = await http.post(
            "/api/v1/auth/2fa/login", json={"challenge_token": challenge, "code": "000000"}
        )
        assert resp.status_code == 400


async def test_2fa_login_rejects_garbage_challenge(
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(db, email="hasfa@x.io", with_2fa=True)
    async with _client() as http:
        resp = await http.post(
            "/api/v1/auth/2fa/login", json={"challenge_token": "not.a.jwt", "code": "123456"}
        )
        assert resp.status_code in (401, 403)


async def test_signin_with_totp_advertises_totp_factor(
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(db, email="hasfa@x.io", with_2fa=True)
    async with _client() as http:
        body = (
            await http.post(
                "/api/v1/auth/signin", json={"email": "hasfa@x.io", "password": _PASSWORD}
            )
        ).json()
        assert body["factors"] == ["totp"]


# ───────────────────────────── email login factor (E5) ─────────────────────────


_EMAIL_CODE = "login-code-1"


@pytest.fixture
def email_2fa_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.email_confirm as ec

    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    monkeypatch.setattr(get_settings(), "email_from", "no-reply@ambersoft.llc")
    monkeypatch.setattr(ec, "_generate_code", lambda: _EMAIL_CODE)

    async def _fake_send(**kw: object) -> None:
        pass

    monkeypatch.setattr(ec, "_send_resend_email", _fake_send)


async def _seed_email_2fa_user(
    db: async_sessionmaker[AsyncSession], *, email: str
) -> None:
    from datetime import UTC, datetime

    from app.models.user_email_2fa import UserEmail2FA

    async with db() as session:
        session.add(
            User(id=1, email=email, hashed_password=hash_password(_PASSWORD), is_active=True)
        )
        session.add(UserEmail2FA(user_id=1, confirmed_at=datetime.now(UTC)))
        await session.commit()


async def test_signin_with_email_2fa_advertises_email_factor(
    db: async_sessionmaker[AsyncSession], email_2fa_on: None
) -> None:
    await _seed_email_2fa_user(db, email="mailfa@x.io")
    async with _client() as http:
        body = (
            await http.post(
                "/api/v1/auth/signin", json={"email": "mailfa@x.io", "password": _PASSWORD}
            )
        ).json()
        assert body["two_factor_required"] is True
        assert body["factors"] == ["email"]
        assert "access_token" not in body


async def test_email_login_happy_path(
    db: async_sessionmaker[AsyncSession], email_2fa_on: None
) -> None:
    await _seed_email_2fa_user(db, email="mailfa@x.io")
    async with _client() as http:
        challenge = (
            await http.post(
                "/api/v1/auth/signin", json={"email": "mailfa@x.io", "password": _PASSWORD}
            )
        ).json()["challenge_token"]

        sent = await http.post(
            "/api/v1/auth/2fa/login/email/request", json={"challenge_token": challenge}
        )
        assert sent.status_code == 200, sent.text
        assert sent.json() == {"sent": True}

        resp = await http.post(
            "/api/v1/auth/2fa/login",
            json={"challenge_token": challenge, "method": "email", "code": _EMAIL_CODE},
        )
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json()["access_token"], str)


async def test_email_login_wrong_code_rejected(
    db: async_sessionmaker[AsyncSession], email_2fa_on: None
) -> None:
    await _seed_email_2fa_user(db, email="mailfa@x.io")
    async with _client() as http:
        challenge = (
            await http.post(
                "/api/v1/auth/signin", json={"email": "mailfa@x.io", "password": _PASSWORD}
            )
        ).json()["challenge_token"]
        await http.post(
            "/api/v1/auth/2fa/login/email/request", json={"challenge_token": challenge}
        )
        resp = await http.post(
            "/api/v1/auth/2fa/login",
            json={"challenge_token": challenge, "method": "email", "code": "wrong-0"},
        )
        assert resp.status_code == 400


async def test_email_login_request_no_enumeration_without_challenge(
    db: async_sessionmaker[AsyncSession], email_2fa_on: None
) -> None:
    # A bad challenge is rejected (401/403) — the endpoint never reveals user state.
    await _seed_email_2fa_user(db, email="mailfa@x.io")
    async with _client() as http:
        resp = await http.post(
            "/api/v1/auth/2fa/login/email/request", json={"challenge_token": "not.a.jwt"}
        )
        assert resp.status_code in (401, 403)
