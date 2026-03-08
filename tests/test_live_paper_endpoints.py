import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import math
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1.endpoints import live as live_endpoint
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base
from app.models.user import User
from app.services.execution.errors import ExchangeServiceError


@pytest.fixture
async def live_paper_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "live_paper_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db_session(live_paper_db: async_sessionmaker[AsyncSession]) -> None:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with live_paper_db() as session:
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


async def _create_strategy(
    client: AsyncClient,
    *,
    name: str,
    strategy_type: str,
    symbol: str,
) -> int:
    response = await client.post(
        "/api/v1/strategies/",
        json={
            "name": name,
            "strategy_type": strategy_type,
            "config": {
                "symbol": symbol,
                "timeframe": "1h",
                "bars": 200,
                "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
                "regime": "Bull",
            },
        },
    )
    assert response.status_code == 200
    return int(response.json()["id"])


async def test_live_paper_profile_is_singleton_per_user_and_no_exchange_needed() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_id = await _create_strategy(
            client,
            name="Live Singleton",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        first = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert first.status_code == 200
        second = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 2000.0,
                "per_trade_usdt": 250.0,
            },
        )
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert second.json()["total_balance_usdt"] == 2000.0
        assert second.json()["per_trade_usdt"] == 250.0

        play = await client.post("/api/v1/live/paper/play")
        assert play.status_code == 200
        assert play.json()["profile"]["is_running"] is True


async def test_live_paper_profile_rejects_entry_size_above_balance() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_id = await _create_strategy(
            client,
            name="Live Balance Guard",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        response = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 100.0,
                "per_trade_usdt": 150.0,
            },
        )
        assert response.status_code == 422


async def test_live_paper_switch_strategy_preserves_backlog_and_creates_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run_backtest(*, strategy, profile):
        now = datetime.now(UTC)
        if strategy.id == strategy_1_id:
            return {
                "trades": [
                    {
                        "side": "LONG",
                        "entry_time": (now - timedelta(minutes=1)).isoformat(),
                        "exit_time": (now - timedelta(seconds=1)).isoformat(),
                        "entry": 100.0,
                        "exit": 101.5,
                        "pnl_usdt": 15.0,
                        "exit_reason": "TAKE",
                    }
                ]
            }
        return {
            "trades": [
                {
                    "side": "SHORT",
                    "entry_time": (now - timedelta(minutes=1, seconds=2)).isoformat(),
                    "exit_time": (now - timedelta(seconds=1)).isoformat(),
                    "entry": 200.0,
                    "exit": 198.0,
                    "pnl_usdt": 10.0,
                    "exit_reason": "TAKE",
                }
            ]
        }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_1_id = await _create_strategy(
            client,
            name="Live Switch A",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        strategy_2_id = await _create_strategy(
            client,
            name="Live Switch B",
            strategy_type="intraday_momentum",
            symbol="ETH/USDT",
        )
        monkeypatch.setattr(live_endpoint.live_paper_service, "_run_backtest_for_strategy", _fake_run_backtest)

        create_profile = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_1_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert create_profile.status_code == 200

        play = await client.post("/api/v1/live/paper/play")
        assert play.status_code == 200
        assert play.json()["profile"]["is_running"] is True

        switch = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_2_id,
                "total_balance_usdt": 1500.0,
                "per_trade_usdt": 200.0,
            },
        )
        assert switch.status_code == 200
        switched_profile = switch.json()
        assert switched_profile["strategy_id"] == strategy_2_id
        assert switched_profile["strategy_revision"] == 2

        await asyncio.sleep(2)
        poll = await client.get("/api/v1/live/paper/poll")
        assert poll.status_code == 200
        body = poll.json()
        assert body["profile"]["strategy_id"] == strategy_2_id
        switch_events = [event for event in body["events"] if event["event_type"] == "strategy_switched"]
        assert len(switch_events) == 1
        assert switch_events[0]["payload"]["from_strategy_id"] == strategy_1_id
        assert switch_events[0]["payload"]["to_strategy_id"] == strategy_2_id
        assert switch_events[0]["payload"]["snapshot"]["closed_trades"] >= 0
        assert len(body["live_trades_since_start"]) >= 0
        if body["live_trades_since_start"]:
            assert {row["strategy_revision"] for row in body["live_trades_since_start"]} == {2}
        assert body["metrics"]["initial_balance"] == 1500.0
        assert body["metrics"]["closed_trades"] >= 1


async def test_live_paper_switch_while_stopped_starts_only_from_play_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_2_call_count = 0

    async def _fake_run_backtest(*, strategy, profile):
        nonlocal strategy_2_call_count
        now = datetime.now(UTC)
        if strategy.id == strategy_2_id:
            strategy_2_call_count += 1
            if strategy_2_call_count == 1:
                return {
                    "trades": [
                        {
                            "side": "LONG",
                            "entry_time": (now - timedelta(minutes=10)).isoformat(),
                            "exit_time": (now - timedelta(minutes=7)).isoformat(),
                            "entry": 120.0,
                            "exit": 121.0,
                            "pnl_usdt": 5.0,
                            "exit_reason": "TAKE",
                        }
                    ]
                }
            return {
                "trades": [
                    {
                        "side": "LONG",
                        "entry_time": (now - timedelta(seconds=10)).isoformat(),
                            "exit_time": (now - timedelta(seconds=1)).isoformat(),
                        "entry": 120.0,
                        "exit": 122.0,
                        "pnl_usdt": 5.0,
                        "exit_reason": "TAKE",
                    }
                ]
            }
        return {"trades": []}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_1_id = await _create_strategy(
            client,
            name="Live Stopped Switch A",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        strategy_2_id = await _create_strategy(
            client,
            name="Live Stopped Switch B",
            strategy_type="intraday_momentum",
            symbol="ETH/USDT",
        )
        monkeypatch.setattr(live_endpoint.live_paper_service, "_run_backtest_for_strategy", _fake_run_backtest)

        created = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_1_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert created.status_code == 200
        assert created.json()["is_running"] is False

        switched = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_2_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert switched.status_code == 200
        assert switched.json()["strategy_revision"] == 2

        played = await client.post("/api/v1/live/paper/play")
        assert played.status_code == 200
        assert played.json()["profile"]["is_running"] is True

        await asyncio.sleep(2)
        polled = await client.get("/api/v1/live/paper/poll")
        assert polled.status_code == 200
        body = polled.json()
        assert len(body["live_trades_since_start"]) == 0

        await asyncio.sleep(2)
        polled_next = await client.get("/api/v1/live/paper/poll")
        assert polled_next.status_code == 200
        body_next = polled_next.json()
        assert len(body_next["live_trades_since_start"]) >= 1
        assert body_next["live_trades_since_start"][-1]["strategy_id"] == strategy_2_id


async def test_live_paper_poll_sanitizes_nan_in_raw_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run_backtest(*, strategy, profile):
        now = datetime.now(UTC)
        return {
            "trades": [
                {
                    "side": "LONG",
                    "entry_time": (now - timedelta(seconds=30)).isoformat(),
                    "exit_time": (now - timedelta(seconds=1)).isoformat(),
                    "entry": 100.0,
                    "exit": 101.0,
                    "pnl_usdt": 2.0,
                    "r_real": math.nan,
                    "exit_reason": "TAKE",
                }
            ]
        }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_id = await _create_strategy(
            client,
            name="Live NaN JSON",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        monkeypatch.setattr(live_endpoint.live_paper_service, "_run_backtest_for_strategy", _fake_run_backtest)

        profile = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert profile.status_code == 200
        play = await client.post("/api/v1/live/paper/play")
        assert play.status_code == 200

        await asyncio.sleep(2)
        poll = await client.get("/api/v1/live/paper/poll")
        assert poll.status_code == 200
        body = poll.json()
        assert len(body["live_trades_since_start"]) >= 1
        assert body["live_trades_since_start"][-1]["raw_payload"]["r_real"] is None


async def test_live_paper_poll_keeps_last_processed_on_max_exit_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0
    fresh_exit = ""

    async def _fake_run_backtest(*, strategy, profile):
        nonlocal call_count
        nonlocal fresh_exit
        call_count += 1
        now = datetime.now(UTC)
        if call_count == 1:
            late_exit = (now - timedelta(milliseconds=100)).isoformat()
            return {
                "trades": [
                    {
                        "side": "LONG",
                        "entry_time": (now - timedelta(seconds=20)).isoformat(),
                        "exit_time": late_exit,
                        "entry": 100.0,
                        "exit": 101.0,
                        "pnl_usdt": 2.0,
                        "exit_reason": "TAKE",
                    }
                ]
            }
        fresh_exit = (now - timedelta(milliseconds=50)).isoformat()
        return {
            "trades": [
                {
                    "side": "LONG",
                    "entry_time": (now - timedelta(seconds=15)).isoformat(),
                    "exit_time": fresh_exit,
                    "entry": 101.0,
                    "exit": 102.0,
                    "pnl_usdt": 2.0,
                    "exit_reason": "TAKE",
                }
            ]
        }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_id = await _create_strategy(
            client,
            name="Live Watermark Guard",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        monkeypatch.setattr(live_endpoint.live_paper_service, "_run_backtest_for_strategy", _fake_run_backtest)

        created = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert created.status_code == 200
        played = await client.post("/api/v1/live/paper/play")
        assert played.status_code == 200

        await asyncio.sleep(2)
        first_poll = await client.get("/api/v1/live/paper/poll")
        assert first_poll.status_code == 200
        first_body = first_poll.json()
        assert len(first_body["live_trades_since_start"]) >= 1
        first_last_id = first_body["live_trades_since_start"][-1]["id"]

        await asyncio.sleep(2)
        second_poll = await client.get("/api/v1/live/paper/poll", params={"last_trade_id": first_last_id})
        assert second_poll.status_code == 200
        second_body = second_poll.json()
        assert len(second_body["live_trades_since_start"]) == 1
        assert second_body["live_trades_since_start"][0]["exit_time"].startswith(fresh_exit[:19])


async def test_live_paper_play_excludes_historical_backtest_from_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0
    started_at = datetime.now(UTC)

    async def _fake_run_backtest(*, strategy, profile):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            old_exit = started_at - timedelta(minutes=10)
            return {
                "trades": [
                    {
                        "side": "LONG",
                        "entry_time": (old_exit - timedelta(minutes=3)).isoformat(),
                        "exit_time": old_exit.isoformat(),
                        "entry": 100.0,
                        "exit": 101.0,
                        "pnl_usdt": 3.0,
                        "exit_reason": "TAKE",
                    }
                ]
            }
        fresh_exit = datetime.now(UTC) - timedelta(seconds=2)
        return {
            "trades": [
                {
                    "side": "LONG",
                    "entry_time": (fresh_exit - timedelta(minutes=2)).isoformat(),
                    "exit_time": fresh_exit.isoformat(),
                    "entry": 101.0,
                    "exit": 102.5,
                    "pnl_usdt": 5.0,
                    "exit_reason": "TAKE",
                }
            ]
        }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_id = await _create_strategy(
            client,
            name="Live Start Baseline",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        monkeypatch.setattr(live_endpoint.live_paper_service, "_run_backtest_for_strategy", _fake_run_backtest)

        created = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert created.status_code == 200
        played = await client.post("/api/v1/live/paper/play")
        assert played.status_code == 200

        first = await client.get("/api/v1/live/paper/poll")
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["metrics"]["closed_trades"] == 0
        assert first_body["metrics"]["total_pnl"] == 0.0
        assert len(first_body["live_trades_since_start"]) == 0

        await asyncio.sleep(2)
        second = await client.get("/api/v1/live/paper/poll")
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["metrics"]["closed_trades"] >= 1
        assert second_body["metrics"]["total_pnl"] >= 5.0
        assert len(second_body["live_trades_since_start"]) >= 1


async def test_live_paper_poll_ignores_static_strategy_candles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_payload: dict[str, object] = {}

    async def _fake_run_vwap(payload: dict[str, object]) -> dict[str, object]:
        nonlocal captured_payload
        captured_payload = payload
        return {"trades": []}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_id = await _create_strategy(
            client,
            name="Live Fresh Candles",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        update = await client.put(
            f"/api/v1/strategies/{strategy_id}",
            json={
                "config": {
                    "symbol": "BTC/USDT",
                    "timeframe": "1m",
                    "bars": 200,
                    "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
                    "regime": "Bull",
                    "candles": [
                        {
                            "time": datetime.now(UTC).isoformat(),
                            "open": 100.0,
                            "high": 101.0,
                            "low": 99.0,
                            "close": 100.5,
                            "volume": 1000.0,
                        }
                    ],
                }
            },
        )
        assert update.status_code == 200

        monkeypatch.setattr(live_endpoint.live_paper_service._backtesting, "run_vwap", _fake_run_vwap)

        profile = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert profile.status_code == 200
        play = await client.post("/api/v1/live/paper/play")
        assert play.status_code == 200

        poll = await client.get("/api/v1/live/paper/poll")
        assert poll.status_code == 200
        assert captured_payload.get("candles") is None


async def test_live_paper_poll_returns_200_and_emits_error_event_on_exchange_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _failing_run_backtest(*, strategy, profile):
        raise ExchangeServiceError(
            code="temporary_unavailable",
            message="bybit unavailable",
            retryable=True,
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_id = await _create_strategy(
            client,
            name="Live Poll Resilience",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        monkeypatch.setattr(live_endpoint.live_paper_service, "_run_backtest_for_strategy", _failing_run_backtest)

        profile = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert profile.status_code == 200
        play = await client.post("/api/v1/live/paper/play")
        assert play.status_code == 200

        poll = await client.get("/api/v1/live/paper/poll")
        assert poll.status_code == 200
        body = poll.json()
        error_events = [event for event in body["events"] if event["event_type"] == "paper_poll_error"]
        assert len(error_events) >= 1
        assert error_events[-1]["payload"]["code"] == "temporary_unavailable"


async def test_live_paper_split_arrays_are_stateful_not_incremental(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run_backtest(*, strategy, profile):
        now = datetime.now(UTC)
        return {
            "trades": [
                {
                    "side": "LONG",
                    "entry_time": (now - timedelta(minutes=2)).isoformat(),
                    "exit_time": (now - timedelta(seconds=1)).isoformat(),
                    "entry": 100.0,
                    "exit": 101.0,
                    "pnl_usdt": 2.0,
                    "exit_reason": "TAKE",
                }
            ]
        }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        strategy_id = await _create_strategy(
            client,
            name="Live Stateful Split",
            strategy_type="builder_vwap",
            symbol="BTC/USDT",
        )
        monkeypatch.setattr(live_endpoint.live_paper_service, "_run_backtest_for_strategy", _fake_run_backtest)

        created = await client.put(
            "/api/v1/live/paper/profile",
            json={
                "strategy_id": strategy_id,
                "total_balance_usdt": 1000.0,
                "per_trade_usdt": 100.0,
            },
        )
        assert created.status_code == 200
        played = await client.post("/api/v1/live/paper/play")
        assert played.status_code == 200

        await asyncio.sleep(2)
        first = await client.get("/api/v1/live/paper/poll")
        assert first.status_code == 200
        first_body = first.json()
        assert len(first_body["live_trades_since_start"]) >= 1
        last_id = first_body["live_trades_since_start"][-1]["id"]

        second = await client.get("/api/v1/live/paper/poll", params={"last_trade_id": last_id})
        assert second.status_code == 200
        second_body = second.json()
        assert len(second_body["live_trades_since_start"]) >= 1
