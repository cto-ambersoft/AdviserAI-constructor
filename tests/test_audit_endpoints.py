from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.base  # noqa: F401
from app.db.session import get_db_session
from app.main import app
from app.models.audit import AuditLog
from app.models.base import Base


async def _signup_token(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "StrongPass123"},
    )
    assert response.status_code == 201
    return response.json()["token"]["access_token"]


def _vwap_candles(count: int = 140) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    price = 100.0
    for i in range(count):
        hour = i % 24
        day = 1 + (i // 24)
        open_price = price
        close_price = price + 0.2
        rows.append(
            {
                "time": f"2025-01-{day:02d}T{hour:02d}:00:00+00:00",
                "open": open_price,
                "high": close_price + 0.4,
                "low": open_price - 0.4,
                "close": close_price,
                "volume": 1000.0 + i,
            }
        )
        price = close_price
    return rows


async def _insert_audit_row(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    actor: str,
    event: str,
    target_type: str,
    target_id: str,
) -> None:
    async with session_factory() as session:
        session.add(
            AuditLog(
                actor=actor,
                event=event,
                reason="seed",
                target_type=target_type,
                target_id=target_id,
                payload={},
            )
        )
        await session.commit()


@pytest.fixture
async def audit_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    db_path = tmp_path / "audit_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, session_factory
    app.dependency_overrides.pop(get_db_session, None)
    await engine.dispose()


async def test_audit_list_returns_own_and_global_only(
    audit_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, session_factory = audit_client
    token_user1 = await _signup_token(client, "audit1@example.com")
    await _signup_token(client, "audit2@example.com")

    await _insert_audit_row(
        session_factory,
        actor="audit1@example.com",
        event="SAVE_STRATEGY",
        target_type="strategy",
        target_id="s1",
    )
    await _insert_audit_row(
        session_factory,
        actor="audit2@example.com",
        event="SAVE_STRATEGY",
        target_type="strategy",
        target_id="s2",
    )
    await _insert_audit_row(
        session_factory,
        actor="system",
        event="BUILDER_CHANGE",
        target_type="system",
        target_id="presets",
    )

    response = await client.get(
        "/api/v1/audit/",
        headers={"Authorization": f"Bearer {token_user1}"},
    )
    assert response.status_code == 200
    body = response.json()
    actors = {row["actor"] for row in body}
    assert "audit1@example.com" in actors
    assert "system" in actors
    assert "audit2@example.com" not in actors


async def test_strategy_create_and_update_write_indicator_audit_events(
    audit_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, session_factory = audit_client
    token = await _signup_token(client, "indicators@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/api/v1/strategies/",
        headers=headers,
        json={
            "name": "My VWAP",
            "strategy_type": "builder_vwap",
            "version": "1.0.0",
            "config": {"enabled": ["VWAP", "MACD"]},
        },
    )
    assert created.status_code == 200
    strategy_id = created.json()["id"]

    updated = await client.put(
        f"/api/v1/strategies/{strategy_id}",
        headers=headers,
        json={"config": {"enabled": ["VWAP", "RSI"]}},
    )
    assert updated.status_code == 200

    async with session_factory() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(
                AuditLog.actor == "indicators@example.com", AuditLog.target_id == str(strategy_id)
            )
            .order_by(AuditLog.created_at.asc())
        )
        events = rows.all()

    event_names = [row.event for row in events]
    assert "SAVE_STRATEGY" in event_names
    assert "UPDATE_STRATEGY" in event_names
    assert "INDICATORS_CHANGE" in event_names
    indicator_event = next(row for row in events if row.event == "INDICATORS_CHANGE")
    assert indicator_event.payload["added"] == ["RSI"]
    assert indicator_event.payload["removed"] == ["MACD"]


async def test_vwap_run_writes_builder_change_audit_event(
    audit_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, session_factory = audit_client
    token = await _signup_token(client, "builder@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/backtest/vwap",
        headers=headers,
        json={
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "bars": 140,
            "candles": _vwap_candles(),
            "enabled": ["VWAP", "MACD"],
            "regime": "Bull",
            "preset": "Custom",
            "include_series": False,
        },
    )
    assert response.status_code == 200

    async with session_factory() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(AuditLog.actor == "builder@example.com", AuditLog.event == "BUILDER_CHANGE")
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        row = rows.first()
    assert row is not None
    assert row.target_type == "backtest"
    assert row.target_id == "vwap"
    assert row.payload["enabled"] == ["VWAP", "MACD"]
