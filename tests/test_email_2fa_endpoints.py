"""E3: email-2FA enrollment endpoints (enroll → confirm → status → disable)."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.email_confirm as ec
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base
from app.models.user import User

_CODE = "enroll-code-1"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e2fa_ep.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            s.add(User(id=1, email="e2fa@x.io", hashed_password="x", is_active=True))
            await s.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def overrides(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    async def _db() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            yield session

    async def _user() -> User:
        return User(id=1, email="e2fa@x.io", hashed_password="x", is_active=True)

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_current_user] = _user
    # Rate-limiter uses real Redis; force fail-open for determinism.
    import app.core.ratelimit as rl

    def _down() -> object:
        raise ConnectionError("redis disabled in tests")

    monkeypatch.setattr(rl, "_get_redis_client", _down)
    yield
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def resend_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    monkeypatch.setattr(get_settings(), "email_from", "no-reply@ambersoft.llc")
    monkeypatch.setattr(ec, "_generate_code", lambda: _CODE)

    async def _fake_send(**kw: object) -> None:
        pass

    monkeypatch.setattr(ec, "_send_resend_email", _fake_send)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _FakeRedisOneShot:
    def __init__(self) -> None:
        self._keys: set[str] = set()

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> object:
        if nx and key in self._keys:
            return None
        self._keys.add(key)
        return True

    async def __aenter__(self) -> "_FakeRedisOneShot":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


async def test_enroll_returns_503_when_resend_disabled() -> None:
    async with _client() as http:
        resp = await http.post("/api/v1/auth/2fa/email/enroll")
    assert resp.status_code == 503


async def test_status_defaults_to_disabled(resend_on: None) -> None:
    async with _client() as http:
        resp = await http.get("/api/v1/auth/2fa/email/status")
    assert resp.json() == {"enabled": False, "available": True}


async def test_enroll_confirm_happy_path(resend_on: None) -> None:
    async with _client() as http:
        enroll = await http.post("/api/v1/auth/2fa/email/enroll")
        assert enroll.status_code == 200, enroll.text
        assert enroll.json() == {"sent": True}
        # not active until confirmed
        assert (await http.get("/api/v1/auth/2fa/email/status")).json()["enabled"] is False

        confirm = await http.post("/api/v1/auth/2fa/email/confirm", json={"code": _CODE})
        assert confirm.status_code == 200, confirm.text
        assert confirm.json() == {"enabled": True, "available": True}
        assert (await http.get("/api/v1/auth/2fa/email/status")).json()["enabled"] is True


async def test_confirm_wrong_code_rejected(resend_on: None) -> None:
    async with _client() as http:
        await http.post("/api/v1/auth/2fa/email/enroll")
        resp = await http.post("/api/v1/auth/2fa/email/confirm", json={"code": "wrong-code"})
        assert resp.status_code == 400
        assert (await http.get("/api/v1/auth/2fa/email/status")).json()["enabled"] is False


async def test_disable_blocked_without_step_up(resend_on: None) -> None:
    async with _client() as http:
        await http.post("/api/v1/auth/2fa/email/enroll")
        await http.post("/api/v1/auth/2fa/email/confirm", json={"code": _CODE})
        # email-2FA confirmed → require_step_up fires → DELETE without token is 403.
        resp = await http.delete("/api/v1/auth/2fa/email")
        assert resp.status_code == 403


async def test_disable_succeeds_with_step_up(
    resend_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.deps as deps_mod
    from app.core.auth import create_step_up_token

    monkeypatch.setattr(deps_mod, "_get_redis_client", lambda: _FakeRedisOneShot())
    async with _client() as http:
        await http.post("/api/v1/auth/2fa/email/enroll")
        await http.post("/api/v1/auth/2fa/email/confirm", json={"code": _CODE})
        token, _ = create_step_up_token(subject="e2fa@x.io")
        resp = await http.delete(
            "/api/v1/auth/2fa/email", headers={"X-Step-Up-Token": token}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False
        assert (await http.get("/api/v1/auth/2fa/email/status")).json()["enabled"] is False


async def test_confirm_locks_out_after_repeated_failures(
    resend_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "totp_max_failed_attempts", 3)
    async with _client() as http:
        await http.post("/api/v1/auth/2fa/email/enroll")
        for _ in range(3):
            r = await http.post("/api/v1/auth/2fa/email/confirm", json={"code": "nope-00"})
            assert r.status_code == 400
        locked = await http.post("/api/v1/auth/2fa/email/confirm", json={"code": "nope-00"})
        assert locked.status_code == 429
        assert "retry-after" in {k.lower() for k in locked.headers}
