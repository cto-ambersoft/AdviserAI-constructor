"""2FA API: enroll / verify / status / disable (B1 / P2-T2)."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base
from app.models.user import User


@pytest.fixture
async def totp_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'totp_endpoints.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(User(id=1, email="totp@x.io", hashed_password="x", is_active=True))
            await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def overrides(totp_db: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with totp_db() as session:
            yield session

    async def _fake_current_user() -> User:
        return User(id=1, email="totp@x.io", hashed_password="x", is_active=True)

    app.dependency_overrides[get_db_session] = _get_test_db_session
    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_current_user, None)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _enable_2fa(http: AsyncClient) -> str:
    secret = (await http.post("/api/v1/auth/2fa/enroll")).json()["secret"]
    await http.post("/api/v1/auth/2fa/verify", json={"code": pyotp.TOTP(secret).now()})
    return secret


async def _step_up_token(http: AsyncClient, secret: str) -> str:
    resp = await http.post("/api/v1/auth/2fa/step-up", json={"code": pyotp.TOTP(secret).now()})
    return resp.json()["step_up_token"]


async def test_enroll_returns_provisioning_uri_and_stays_disabled() -> None:
    async with _client() as http:
        resp = await http.post("/api/v1/auth/2fa/enroll")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provisioning_uri"].startswith("otpauth://totp/")
        assert body["secret"]

        status = await http.get("/api/v1/auth/2fa/status")
        assert status.json()["enabled"] is False  # not active until verified


async def test_verify_enables_2fa() -> None:
    async with _client() as http:
        secret = (await http.post("/api/v1/auth/2fa/enroll")).json()["secret"]
        code = pyotp.TOTP(secret).now()

        resp = await http.post("/api/v1/auth/2fa/verify", json={"code": code})
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is True
        assert (await http.get("/api/v1/auth/2fa/status")).json()["enabled"] is True


async def test_verify_wrong_code_is_rejected() -> None:
    async with _client() as http:
        await http.post("/api/v1/auth/2fa/enroll")
        resp = await http.post("/api/v1/auth/2fa/verify", json={"code": "000000"})
        assert resp.status_code == 400
        assert (await http.get("/api/v1/auth/2fa/status")).json()["enabled"] is False


async def test_status_defaults_to_disabled() -> None:
    async with _client() as http:
        assert (await http.get("/api/v1/auth/2fa/status")).json()["enabled"] is False


async def test_enroll_conflicts_when_already_enabled() -> None:
    async with _client() as http:
        secret = (await http.post("/api/v1/auth/2fa/enroll")).json()["secret"]
        await http.post("/api/v1/auth/2fa/verify", json={"code": pyotp.TOTP(secret).now()})
        # Re-enrolling while active would silently disable 2FA — must be blocked.
        resp = await http.post("/api/v1/auth/2fa/enroll")
        assert resp.status_code == 409
        assert (await http.get("/api/v1/auth/2fa/status")).json()["enabled"] is True


async def test_delete_disables_2fa_with_step_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # Step-up single-use is now fail-CLOSED (T5/S8): with Redis available the jti is
    # recorded and the action succeeds.
    import app.api.deps as deps_mod

    monkeypatch.setattr(deps_mod, "_get_redis_client", lambda: _FakeRedisOneShot())
    async with _client() as http:
        secret = await _enable_2fa(http)
        token = await _step_up_token(http, secret)

        resp = await http.delete("/api/v1/auth/2fa", headers={"X-Step-Up-Token": token})
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False
        assert (await http.get("/api/v1/auth/2fa/status")).json()["enabled"] is False


# ─────────────────────────── recovery codes (P2-T4) ───────────────────────────


async def test_enroll_response_includes_recovery_codes() -> None:
    async with _client() as http:
        body = (await http.post("/api/v1/auth/2fa/enroll")).json()
        assert isinstance(body["recovery_codes"], list)
        assert len(body["recovery_codes"]) == 10


async def test_verify_endpoint_does_not_consume_recovery_code() -> None:
    # I2 — /2fa/verify is TOTP-only; a recovery code submitted there is rejected and
    # must remain available for step-up.
    async with _client() as http:
        enroll = (await http.post("/api/v1/auth/2fa/enroll")).json()
        secret, recovery = enroll["secret"], enroll["recovery_codes"][0]
        await http.post("/api/v1/auth/2fa/verify", json={"code": pyotp.TOTP(secret).now()})

        rejected = await http.post("/api/v1/auth/2fa/verify", json={"code": recovery})
        assert rejected.status_code == 400
        # Not consumed → still works for step-up.
        step_up = await http.post("/api/v1/auth/2fa/step-up", json={"code": recovery})
        assert step_up.status_code == 200, step_up.text


async def test_recovery_code_works_for_step_up_once() -> None:
    async with _client() as http:
        enroll = (await http.post("/api/v1/auth/2fa/enroll")).json()
        secret, recovery = enroll["secret"], enroll["recovery_codes"][0]
        await http.post("/api/v1/auth/2fa/verify", json={"code": pyotp.TOTP(secret).now()})

        resp = await http.post("/api/v1/auth/2fa/step-up", json={"code": recovery})
        assert resp.status_code == 200, resp.text
        assert resp.json()["step_up_token"]
        # one-time: reuse fails
        reuse = await http.post("/api/v1/auth/2fa/step-up", json={"code": recovery})
        assert reuse.status_code == 400


# ──────────────────────────── step-up (P2-T3) ────────────────────────────


async def test_verify_locks_out_after_repeated_failures() -> None:
    # C1 — unlimited TOTP guesses are the second-factor's weak point; lock out after
    # the configured number of failures (default 5).
    async with _client() as http:
        secret = (await http.post("/api/v1/auth/2fa/enroll")).json()["secret"]
        await http.post("/api/v1/auth/2fa/verify", json={"code": pyotp.TOTP(secret).now()})

        for _ in range(5):
            r = await http.post("/api/v1/auth/2fa/verify", json={"code": "000000"})
            assert r.status_code == 400
        locked = await http.post("/api/v1/auth/2fa/verify", json={"code": "000000"})
        assert locked.status_code == 429
        assert "retry-after" in {k.lower() for k in locked.headers}


async def test_step_up_requires_2fa_enabled() -> None:
    async with _client() as http:
        resp = await http.post("/api/v1/auth/2fa/step-up", json={"code": "123456"})
        assert resp.status_code == 400


async def test_step_up_returns_token_with_valid_code() -> None:
    async with _client() as http:
        secret = await _enable_2fa(http)
        resp = await http.post(
            "/api/v1/auth/2fa/step-up", json={"code": pyotp.TOTP(secret).now()}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["step_up_token"]
        assert body["expires_in"] > 0


async def test_step_up_rejects_wrong_code() -> None:
    async with _client() as http:
        await _enable_2fa(http)
        resp = await http.post("/api/v1/auth/2fa/step-up", json={"code": "000000"})
        assert resp.status_code == 400


async def test_disable_blocked_without_step_up_when_2fa_enabled() -> None:
    async with _client() as http:
        await _enable_2fa(http)
        resp = await http.delete("/api/v1/auth/2fa")  # no X-Step-Up-Token
        assert resp.status_code == 403


async def test_critical_action_passthrough_without_2fa() -> None:
    # No enrollment → step-up not required → the gated action proceeds.
    async with _client() as http:
        resp = await http.delete("/api/v1/auth/2fa")
        assert resp.status_code == 200


async def test_critical_action_blocked_without_2fa_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # I8: with step_up_require_2fa on, a no-2FA user is refused the gated action.
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "step_up_require_2fa", True)
    async with _client() as http:
        resp = await http.delete("/api/v1/auth/2fa")
        assert resp.status_code == 403


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


async def test_step_up_token_is_single_use(monkeypatch: pytest.MonkeyPatch) -> None:
    # I4 — one re-auth authorizes one action; replaying the step-up token is rejected.
    import app.api.deps as deps_mod

    fake = _FakeRedisOneShot()  # one shared store across both requests
    monkeypatch.setattr(deps_mod, "_get_redis_client", lambda: fake)
    async with _client() as http:
        secret = await _enable_2fa(http)
        token = await _step_up_token(http, secret)

        # First use consumes the token; the handler fails for unrelated reasons (no
        # config) but NOT with a step-up 403.
        first = await http.post(
            "/api/v1/live/auto-trade/play", headers={"X-Step-Up-Token": token}
        )
        assert first.status_code != 403
        # Replay of the same token is rejected.
        second = await http.post(
            "/api/v1/live/auto-trade/play", headers={"X-Step-Up-Token": token}
        )
        assert second.status_code == 403


async def test_step_up_token_for_another_user_is_rejected() -> None:
    # I6 — require_step_up must reject a valid step-up token minted for a different
    # subject than the authenticated user (no cross-user replay).
    from app.core.auth import create_step_up_token

    async with _client() as http:
        await _enable_2fa(http)  # current user is totp@x.io
        other_token, _ = create_step_up_token(subject="someone-else@x.io")
        resp = await http.delete("/api/v1/auth/2fa", headers={"X-Step-Up-Token": other_token})
        assert resp.status_code == 403


async def test_start_auto_trade_blocked_without_step_up_when_2fa_enabled() -> None:
    # The step-up dependency fires before the handler, so no auto-trade config is
    # needed — a 2FA-enabled user without a step-up token is blocked outright.
    async with _client() as http:
        await _enable_2fa(http)
        resp = await http.post("/api/v1/live/auto-trade/play")
        assert resp.status_code == 403
