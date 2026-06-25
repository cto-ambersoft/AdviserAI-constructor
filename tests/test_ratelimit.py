"""T5 (S7): fixed-window Redis rate limiter for the login endpoints. Fails OPEN on
a Redis outage (availability over strict limiting for auth), keyed independently."""

import pytest

from app.core import ratelimit as rl


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    async def __aenter__(self) -> "_FakeRedis":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> bool:
        return True


async def test_allows_until_limit_then_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl, "_get_redis_client", lambda: _FakeRedis())
    # Same client instance across calls via closure
    fake = _FakeRedis()
    monkeypatch.setattr(rl, "_get_redis_client", lambda: fake)
    results = [await rl.check_rate_limit("k", limit=3, window_seconds=60) for _ in range(4)]
    assert results == [True, True, True, False]


async def test_fail_open_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> _FakeRedis:
        raise ConnectionError("redis down")

    monkeypatch.setattr(rl, "_get_redis_client", _boom)
    assert await rl.check_rate_limit("k", limit=1, window_seconds=60) is True


async def test_independent_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(rl, "_get_redis_client", lambda: fake)
    assert await rl.check_rate_limit("a", limit=1, window_seconds=60) is True
    assert await rl.check_rate_limit("a", limit=1, window_seconds=60) is False
    assert await rl.check_rate_limit("b", limit=1, window_seconds=60) is True
