from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base
from app.models.user import User


@pytest.fixture
async def strategy_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "strategies_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db_session(strategy_db: async_sessionmaker[AsyncSession]) -> None:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with strategy_db() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session
    yield
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture(autouse=True)
def override_current_user() -> None:
    async def _fake_current_user() -> User:
        return User(id=1, email="test@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def test_update_strategy_by_id_updates_existing_record() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/strategies/",
            json={
                "name": "VWAP Base",
                "strategy_type": "builder_vwap",
                "version": "1.0.0",
                "config": {"enabled": ["VWAP"]},
            },
        )
        assert created.status_code == 200
        strategy_id = created.json()["id"]

        updated = await client.put(
            f"/api/v1/strategies/{strategy_id}",
            json={
                "name": "VWAP Base",
                "config": {"enabled": ["VWAP", "MACD"], "rr": 3.0},
                "description": "updated config",
            },
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["id"] == strategy_id
        assert body["description"] == "updated config"
        assert body["config"]["rr"] == 3.0

        listed = await client.get("/api/v1/strategies/")
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["id"] == strategy_id
        assert rows[0]["config"]["enabled"] == ["VWAP", "MACD"]


async def test_update_strategy_returns_404_for_unknown_id() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/v1/strategies/9999", json={"name": "Nope"})
    assert response.status_code == 404
