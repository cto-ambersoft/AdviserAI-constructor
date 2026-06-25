"""E4: step-up via email — an emailed code mints a factor-agnostic step-up token."""

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

_CODE = "stepup-code-1"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e2fa_su.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            s.add(User(id=1, email="su@x.io", hashed_password="x", is_active=True))
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
        return User(id=1, email="su@x.io", hashed_password="x", is_active=True)

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_current_user] = _user
    import app.core.ratelimit as rl

    def _down() -> object:
        raise ConnectionError("redis disabled in tests")

    monkeypatch.setattr(rl, "_get_redis_client", _down)
    # email step-up is enabled via Resend config + a fixed code.
    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    monkeypatch.setattr(get_settings(), "email_from", "no-reply@ambersoft.llc")
    monkeypatch.setattr(ec, "_generate_code", lambda: _CODE)

    async def _fake_send(**kw: object) -> None:
        pass

    monkeypatch.setattr(ec, "_send_resend_email", _fake_send)
    yield
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_current_user, None)


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


async def _enable_email_2fa(http: AsyncClient) -> None:
    await http.post("/api/v1/auth/2fa/email/enroll")
    await http.post("/api/v1/auth/2fa/email/confirm", json={"code": _CODE})


async def test_step_up_email_request_requires_enrollment() -> None:
    async with _client() as http:
        resp = await http.post("/api/v1/auth/2fa/step-up/email/request")
        assert resp.status_code == 400  # not enrolled


async def test_email_step_up_mints_token_that_passes_a_gated_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.deps as deps_mod

    monkeypatch.setattr(deps_mod, "_get_redis_client", lambda: _FakeRedisOneShot())
    async with _client() as http:
        await _enable_email_2fa(http)

        req = await http.post("/api/v1/auth/2fa/step-up/email/request")
        assert req.status_code == 200, req.text
        assert req.json() == {"sent": True}

        su = await http.post(
            "/api/v1/auth/2fa/step-up", json={"method": "email", "code": _CODE}
        )
        assert su.status_code == 200, su.text
        token = su.json()["step_up_token"]
        assert token

        # The minted token authorizes a gated action (no step-up 403).
        gated = await http.post(
            "/api/v1/live/auto-trade/play", headers={"X-Step-Up-Token": token}
        )
        assert gated.status_code != 403


async def test_email_step_up_rejects_wrong_code() -> None:
    async with _client() as http:
        await _enable_email_2fa(http)
        await http.post("/api/v1/auth/2fa/step-up/email/request")
        resp = await http.post(
            "/api/v1/auth/2fa/step-up", json={"method": "email", "code": "wrong-0"}
        )
        assert resp.status_code == 400


async def test_email_step_up_requires_enrollment() -> None:
    async with _client() as http:
        resp = await http.post(
            "/api/v1/auth/2fa/step-up", json={"method": "email", "code": _CODE}
        )
        assert resp.status_code == 400  # email-2FA not enabled
