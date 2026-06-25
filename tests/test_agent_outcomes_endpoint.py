"""T10a (W3a): internal endpoint exposing REAL trade outcomes keyed by the core
ai_decision_events id (auto_trade_position.decision_event_id). The core service joins
on this to compute agent accuracy from actual closed trades instead of a synthetic
daily-price move.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import app
from app.models.auto_trade_position import AutoTradePosition
from app.models.base import Base

_KEY = "test-internal-key"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outcomes.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    async def _get_test_db() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db
    monkeypatch.setattr(get_settings(), "internal_api_key", _KEY)
    yield
    app.dependency_overrides.pop(get_db_session, None)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _position(**overrides: object) -> AutoTradePosition:
    base: dict[str, object] = dict(
        user_id=1,
        account_id=1,
        config_id=1,
        profile_id=1,
        symbol="BTCUSDT",
        side="LONG",
        status="closed",
        entry_price=100.0,
        quantity=0.1,
        position_size_usdt=100.0,
        tp_price=110.0,
        sl_price=95.0,
        entry_confidence_pct=70.0,
        leverage=1,
        opened_at=datetime.now(UTC) - timedelta(days=1),
        closed_at=datetime.now(UTC),
        close_price=110.0,
        decision_event_id="evt-1",
    )
    base.update(overrides)
    return AutoTradePosition(**base)


async def test_returns_realized_move_for_closed_positions_with_decision_id(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        session.add(_position(decision_event_id="evt-1", entry_price=100.0, close_price=110.0))
        # excluded: no decision_event_id
        session.add(_position(decision_event_id=None, close_price=120.0))
        # excluded: still open
        session.add(_position(decision_event_id="evt-open", status="closed", close_price=None))
        await session.commit()

    async with _client() as http:
        resp = await http.get(
            "/api/v1/internal/agent-outcomes",
            params={"since_days": 30},
            headers={"X-Internal-API-Key": _KEY},
        )
    assert resp.status_code == 200, resp.text
    outcomes = resp.json()["outcomes"]
    by_id = {o["decision_event_id"]: o for o in outcomes}
    assert set(by_id) == {"evt-1"}
    assert by_id["evt-1"]["realized_move_pct"] == pytest.approx(10.0)
    assert by_id["evt-1"]["symbol"] == "BTCUSDT"


async def test_short_side_realized_move_is_raw_market_move(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # The realized move is the raw entry->close market move; the agent's signal
    # direction (long/short/flat) is judged against it just like the synthetic path.
    async with db() as session:
        session.add(
            _position(decision_event_id="evt-s", side="SHORT", entry_price=100.0, close_price=90.0)
        )
        await session.commit()
    async with _client() as http:
        resp = await http.get(
            "/api/v1/internal/agent-outcomes",
            headers={"X-Internal-API-Key": _KEY},
        )
    assert resp.status_code == 200
    assert resp.json()["outcomes"][0]["realized_move_pct"] == pytest.approx(-10.0)


async def test_requires_internal_key() -> None:
    async with _client() as http:
        resp = await http.get("/api/v1/internal/agent-outcomes")
    assert resp.status_code == 401
