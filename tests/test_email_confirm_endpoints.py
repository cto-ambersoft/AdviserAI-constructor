"""T20 (W11c): /auth/email-confirm request + verify endpoints."""

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


@pytest.fixture
async def ec_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ec_ep.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            s.add(User(id=1, email="ec@x.io", hashed_password="x", is_active=True))
            await s.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def overrides(
    ec_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    async def _db() -> AsyncIterator[AsyncSession]:
        async with ec_db() as session:
            yield session

    async def _user() -> User:
        return User(id=1, email="ec@x.io", hashed_password="x", is_active=True)

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


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_request_returns_503_when_disabled() -> None:
    async with _client() as http:
        resp = await http.post("/api/v1/auth/email-confirm/request", json={"action": "x"})
    assert resp.status_code == 503


async def test_request_then_verify_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    monkeypatch.setattr(get_settings(), "email_from", "no-reply@ambersoft.llc")
    monkeypatch.setattr(ec, "_generate_code", lambda: "424242")

    sent: list[dict[str, object]] = []

    async def _fake_send(**kw: object) -> None:
        sent.append(kw)

    monkeypatch.setattr(ec, "_send_resend_email", _fake_send)

    async with _client() as http:
        req = await http.post(
            "/api/v1/auth/email-confirm/request", json={"action": "change_exchange_key"}
        )
        assert req.status_code == 200, req.text
        assert req.json() == {"sent": True, "enabled": True}
        assert len(sent) == 1 and sent[0]["to"] == "ec@x.io"

        ok = await http.post(
            "/api/v1/auth/email-confirm/verify",
            json={"action": "change_exchange_key", "code": "424242"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"confirmed": True}

        reuse = await http.post(
            "/api/v1/auth/email-confirm/verify",
            json={"action": "change_exchange_key", "code": "424242"},
        )
        assert reuse.json() == {"confirmed": False}


async def test_verify_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    # C1: brute-forcing verify is throttled (429) per (user, action).
    import app.core.ratelimit as rl

    class _CountingRedis:
        store: dict[str, int] = {}

        async def __aenter__(self) -> "_CountingRedis":
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def incr(self, key: str) -> int:
            type(self).store[key] = type(self).store.get(key, 0) + 1
            return type(self).store[key]

        async def expire(self, key: str, ttl: int) -> bool:
            return True

    _CountingRedis.store = {}
    monkeypatch.setattr(rl, "_get_redis_client", lambda: _CountingRedis())
    monkeypatch.setattr(get_settings(), "login_rate_limit_max_attempts", 2)
    monkeypatch.setattr(get_settings(), "login_rate_limit_window_seconds", 60)

    async with _client() as http:
        codes = [
            await http.post(
                "/api/v1/auth/email-confirm/verify",
                json={"action": "act", "code": "000000"},
            )
            for _ in range(3)
        ]
    assert [r.status_code for r in codes] == [200, 200, 429]
