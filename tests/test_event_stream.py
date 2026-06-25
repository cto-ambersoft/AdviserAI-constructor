"""SSE event channel (B3 / P2-T6): publish filter, emit hook, /events/stream."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.api.v1.endpoints.events as events_endpoint
import app.services.events.stream as stream_mod
from app.api.deps import get_current_user
from app.main import app
from app.models.auto_trade_event import AutoTradeEvent
from app.models.base import Base
from app.models.user import User
from app.services.auto_trade.service import AutoTradeService
from app.services.events.stream import STREAMABLE_EVENTS, publish_user_event, user_channel


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, body: str) -> None:
        self.published.append((channel, body))

    async def __aenter__(self) -> "_FakeRedis":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


# ───────────────────────────── publish filter ─────────────────────────────


async def test_publish_skips_non_streamable_event(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(stream_mod, "_get_redis_client", lambda: fake)
    await publish_user_event(user_id=1, event_type="position_opened", payload={}, message=None)
    assert fake.published == []


async def test_publish_streamable_event_to_user_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(stream_mod, "_get_redis_client", lambda: fake)
    await publish_user_event(
        user_id=7, event_type="kpi_guard_triggered", payload={"rule": "max_dd"}, message="breach"
    )
    assert len(fake.published) == 1
    channel, body = fake.published[0]
    assert channel == user_channel(7)
    data = json.loads(body)
    assert data["event_type"] == "kpi_guard_triggered"
    assert data["payload"] == {"rule": "max_dd"}
    assert data["message"] == "breach"


async def test_publish_never_raises_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> _FakeRedis:
        raise ConnectionError("redis down")

    monkeypatch.setattr(stream_mod, "_get_redis_client", _boom)
    # Best-effort: a Redis outage must not propagate into the trade path.
    await publish_user_event(user_id=1, event_type="portfolio_dd_halt", payload={}, message=None)


async def test_publish_tolerates_non_json_native_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    fake = _FakeRedis()
    monkeypatch.setattr(stream_mod, "_get_redis_client", lambda: fake)
    # A Decimal isn't JSON-native; serialization must not raise into the trade path.
    await publish_user_event(
        user_id=1, event_type="portfolio_dd_halt", payload={"x": Decimal("1.5")}, message=None
    )
    assert len(fake.published) == 1
    assert "1.5" in fake.published[0][1]


def test_streamable_events_cover_the_risk_family() -> None:
    for evt in ("kpi_guard_triggered", "kill_switch_triggered", "portfolio_dd_halt", "data_stale"):
        assert evt in STREAMABLE_EVENTS
    assert "position_opened" not in STREAMABLE_EVENTS


# ─────────────────────────── _emit_event hook ───────────────────────────


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as s:
            yield s
    finally:
        await engine.dispose()


async def _record_publishes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    recorded: list[tuple[int, str]] = []

    async def _fake_publish(
        *, user_id: int, event_type: str, payload: dict, message: str | None
    ) -> None:
        recorded.append((user_id, event_type))

    # Patch where the after-commit listener resolves it.
    monkeypatch.setattr(stream_mod, "publish_user_event", _fake_publish)
    return recorded


async def _emit(
    session: AsyncSession, *, event_type: str, commit: bool, user_id: int = 3
) -> None:
    await AutoTradeService()._emit_event(
        session=session,
        user_id=user_id,
        config_id=None,
        profile_id=None,
        history_id=None,
        position_id=None,
        event_type=event_type,
        level="warning",
        message="m",
        payload={"worst_dd_pct": 25.0},
        commit=commit,
    )


async def test_emit_event_publishes_streamable_after_commit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = await _record_publishes(monkeypatch)
    await _emit(session, event_type="portfolio_dd_halt", commit=True)
    await asyncio.sleep(0)  # let the after-commit-scheduled publish run

    assert (3, "portfolio_dd_halt") in recorded
    row = await session.scalar(
        select(AutoTradeEvent).where(AutoTradeEvent.event_type == "portfolio_dd_halt")
    )
    assert row is not None


async def test_emit_event_does_not_publish_on_rollback(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I1 — a streamable event emitted with commit=False that is then rolled back must
    # never reach the live stream (no phantom event).
    recorded = await _record_publishes(monkeypatch)
    await _emit(session, event_type="kpi_guard_triggered", commit=False)
    await session.rollback()
    await asyncio.sleep(0)

    assert recorded == []


# ──────────────────────────── /events/stream ────────────────────────────


@pytest.fixture(autouse=True)
def override_current_user() -> Iterator[None]:
    async def _fake_current_user() -> User:
        return User(id=1, email="sse@x.io", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def test_sse_stream_rejects_over_connection_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # S1 — a user already at the per-worker stream cap is refused with 429.
    monkeypatch.setitem(
        events_endpoint._active_streams, 1, events_endpoint._max_streams_per_user()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get("/api/v1/events/stream")
        assert resp.status_code == 429


async def test_sse_stream_yields_user_events(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_subscribe(user_id: int) -> AsyncIterator[dict]:
        yield {"event_type": "kpi_guard_triggered", "payload": {"rule": "max_dd"}, "message": "b"}
        yield {"event_type": "portfolio_dd_halt", "payload": {"worst_dd_pct": 25.0}, "message": "h"}

    monkeypatch.setattr(events_endpoint, "subscribe_user_events", _fake_subscribe)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get("/api/v1/events/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "kpi_guard_triggered" in body
        assert "portfolio_dd_halt" in body
        assert "max_dd" in body
