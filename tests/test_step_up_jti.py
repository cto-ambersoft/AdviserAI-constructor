"""T5 (S8): step-up single-use must fail CLOSED — if Redis is unavailable the
jti cannot be marked used, so the action must be denied rather than letting a
step-up token be replayed within its TTL.
"""

import pytest

from app.api import deps


async def test_consume_step_up_jti_denies_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> object:
        raise ConnectionError("redis down")

    monkeypatch.setattr(deps, "_get_redis_client", _boom)
    assert await deps._consume_step_up_jti("some-jti") is False


async def test_consume_step_up_jti_allows_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRedis:
        def __init__(self) -> None:
            self.keys: set[str] = set()

        async def __aenter__(self) -> "_FakeRedis":
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool | None:
            if nx and key in self.keys:
                return None
            self.keys.add(key)
            return True

    fake = _FakeRedis()
    monkeypatch.setattr(deps, "_get_redis_client", lambda: fake)
    assert await deps._consume_step_up_jti("jti-1") is True
    assert await deps._consume_step_up_jti("jti-1") is False  # replay rejected
