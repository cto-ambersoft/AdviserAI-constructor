from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1.endpoints.live import auto_trade_service
from app.db.session import get_db_session
from app.main import app
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User


class _NoopTradingService:
    async def fetch_futures_position(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
    ):
        return None

    async def fetch_futures_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        since=None,
        limit: int = 200,
    ) -> list[object]:
        return []


@pytest.fixture(autouse=True)
def override_auto_trade_exchange_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _db_only_get_open_position(
        *, session: AsyncSession, user_id: int, account_id: int | None = None
    ):
        stmt = select(AutoTradePosition).where(
            AutoTradePosition.user_id == user_id,
            AutoTradePosition.status == "open",
        )
        if account_id is not None:
            stmt = stmt.where(AutoTradePosition.account_id == account_id)
        return await session.scalar(stmt.order_by(AutoTradePosition.id.desc()).limit(1))

    monkeypatch.setattr(auto_trade_service, "_trading", _NoopTradingService())
    monkeypatch.setattr(auto_trade_service, "get_open_position", _db_only_get_open_position)
    async def _noop_sync_positions_snapshot_for_user(
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int | None,
        history_id: int | None,
        emit_events: bool,
        close_missing_on_exchange: bool,
    ) -> None:
        return None

    monkeypatch.setattr(
        auto_trade_service,
        "_sync_positions_snapshot_for_user",
        _noop_sync_positions_snapshot_for_user,
    )
    yield


@pytest.fixture
async def auto_trade_endpoints_db(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "auto_trade_endpoints.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db_session(auto_trade_endpoints_db: async_sessionmaker[AsyncSession]) -> None:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with auto_trade_endpoints_db() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session
    yield
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture(autouse=True)
def override_current_user() -> None:
    async def _fake_current_user() -> User:
        return User(id=1, email="auto-endpoints@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_profile_and_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    async with session_factory() as session:
        user = User(id=1, email="auto-endpoints@example.com", hashed_password="x", is_active=True)
        session.add(user)
        profile = PersonalAnalysisProfile(
            user_id=1,
            symbol="BTCUSDT",
            query_prompt=None,
            agents={"twitterSentiment": True},
            agent_weights={"twitterSentiment": 1.0},
            interval_minutes=60,
            is_active=True,
            next_run_at=datetime.now(UTC),
            last_triggered_at=None,
            last_completed_at=None,
        )
        session.add(profile)
        await session.flush()
        account = ExchangeCredential(
            user_id=1,
            exchange_name="bybit",
            account_label="main",
            mode="demo",
            encrypted_api_key="k",
            encrypted_api_secret="s",
            encrypted_passphrase=None,
        )
        session.add(account)
        await session.commit()
        await session.refresh(profile)
        await session.refresh(account)
        return profile.id, account.id


async def _seed_foreign_profile_and_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    async with session_factory() as session:
        user = User(id=2, email="foreign@example.com", hashed_password="x", is_active=True)
        session.add(user)
        profile = PersonalAnalysisProfile(
            user_id=2,
            symbol="ETHUSDT",
            query_prompt=None,
            agents={"twitterSentiment": True},
            agent_weights={"twitterSentiment": 1.0},
            interval_minutes=60,
            is_active=True,
            next_run_at=datetime.now(UTC),
            last_triggered_at=None,
            last_completed_at=None,
        )
        session.add(profile)
        await session.flush()
        account = ExchangeCredential(
            user_id=2,
            exchange_name="bybit",
            account_label="foreign",
            mode="demo",
            encrypted_api_key="k2",
            encrypted_api_secret="s2",
            encrypted_passphrase=None,
        )
        session.add(account)
        await session.commit()
        await session.refresh(profile)
        await session.refresh(account)
        return profile.id, account.id


async def _seed_second_profile_and_account_for_user1(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    async with session_factory() as session:
        profile = PersonalAnalysisProfile(
            user_id=1,
            symbol="ETHUSDT",
            query_prompt=None,
            agents={"twitterSentiment": True},
            agent_weights={"twitterSentiment": 1.0},
            interval_minutes=60,
            is_active=True,
            next_run_at=datetime.now(UTC),
            last_triggered_at=None,
            last_completed_at=None,
        )
        session.add(profile)
        await session.flush()
        account = ExchangeCredential(
            user_id=1,
            exchange_name="bybit",
            account_label="secondary",
            mode="demo",
            encrypted_api_key="k-secondary",
            encrypted_api_secret="s-secondary",
            encrypted_passphrase=None,
        )
        session.add(account)
        await session.commit()
        await session.refresh(profile)
        await session.refresh(account)
        return profile.id, account.id


async def _seed_open_auto_trade_position(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    profile_id: int,
    account_id: int,
) -> int:
    async with session_factory() as session:
        config = AutoTradeConfig(
            user_id=1,
            profile_id=profile_id,
            account_id=account_id,
            enabled=True,
            is_running=True,
            position_size_usdt=100.0,
            leverage=2,
            min_confidence_pct=62.0,
            fast_close_confidence_pct=80.0,
            confirm_reports_required=2,
            risk_mode="1:2",
            sl_pct=1.0,
            tp_pct=2.0,
            last_started_at=datetime.now(UTC),
            last_stopped_at=None,
        )
        session.add(config)
        await session.flush()
        position = AutoTradePosition(
            user_id=1,
            config_id=config.id,
            profile_id=profile_id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            side="LONG",
            status="open",
            entry_price=100.0,
            quantity=1.0,
            position_size_usdt=100.0,
            leverage=2,
            tp_price=102.0,
            sl_price=99.0,
            entry_confidence_pct=70.0,
            opened_at=datetime.now(UTC),
            closed_at=None,
            close_reason=None,
            close_price=None,
            open_order_id="ord-open-1",
            close_order_id=None,
            open_history_id=None,
            close_history_id=None,
            raw_open_order={},
            raw_close_order={},
        )
        session.add(position)
        await session.commit()
        await session.refresh(position)
        return position.id


async def test_auto_trade_config_play_state_and_events_endpoints(
    auto_trade_endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    profile_id, account_id = await _seed_profile_and_account(auto_trade_endpoints_db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.put(
            "/api/v1/live/auto-trade/config",
            json={
                "enabled": True,
                "profile_id": profile_id,
                "account_id": account_id,
                "position_size_usdt": 100.0,
                "leverage": 1,
                "min_confidence_pct": 62.0,
                "fast_close_confidence_pct": 80.0,
                "confirm_reports_required": 2,
                "risk_mode": "1:2",
                "sl_pct": 1.0,
                "tp_pct": 2.5,
            },
        )
        assert invalid.status_code == 422

        created = await client.put(
            "/api/v1/live/auto-trade/config",
            json={
                "enabled": True,
                "profile_id": profile_id,
                "account_id": account_id,
                "position_size_usdt": 100.0,
                "leverage": 1,
                "min_confidence_pct": 62.0,
                "fast_close_confidence_pct": 80.0,
                "confirm_reports_required": 2,
                "risk_mode": "1:2",
                "sl_pct": 1.0,
                "tp_pct": 2.0,
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["enabled"] is True
        assert body["is_running"] is False
        assert body["profile_id"] == profile_id

        fetched = await client.get("/api/v1/live/auto-trade/config")
        assert fetched.status_code == 200
        assert fetched.json()["account_id"] == account_id

        played = await client.post("/api/v1/live/auto-trade/play")
        assert played.status_code == 200
        assert played.json()["config"]["is_running"] is True

        state = await client.get("/api/v1/live/auto-trade/state")
        assert state.status_code == 200
        state_body = state.json()
        assert state_body["config"]["is_running"] is True

        events = await client.get("/api/v1/live/auto-trade/events")
        assert events.status_code == 200
        event_types = {item["event_type"] for item in events.json()["events"]}
        assert "config_created" in event_types or "config_updated" in event_types
        assert "auto_trade_play" in event_types


async def test_auto_trade_positions_endpoint_returns_empty_summary() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/live/auto-trade/positions")
    assert response.status_code == 200
    body = response.json()
    assert body["positions"] == []
    assert body["summary"] == {
        "total_positions": 0,
        "open_positions": 0,
        "closed_positions": 0,
        "total_realized_pnl_usdt": 0.0,
        "total_unrealized_pnl_usdt": 0.0,
        "total_pnl_usdt": 0.0,
        "total_trade_pnl_usdt": 0.0,
    }


async def test_auto_trade_configs_endpoint_lists_all_user_configs(
    auto_trade_endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    profile1_id, account1_id = await _seed_profile_and_account(auto_trade_endpoints_db)
    profile2_id, account2_id = await _seed_second_profile_and_account_for_user1(
        auto_trade_endpoints_db
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for cfg_profile_id, cfg_account_id in (
            (profile1_id, account1_id),
            (profile2_id, account2_id),
        ):
            created = await client.put(
                "/api/v1/live/auto-trade/config",
                json={
                    "enabled": True,
                    "profile_id": cfg_profile_id,
                    "account_id": cfg_account_id,
                    "position_size_usdt": 100.0,
                    "leverage": 1,
                    "min_confidence_pct": 62.0,
                    "fast_close_confidence_pct": 80.0,
                    "confirm_reports_required": 2,
                    "risk_mode": "1:2",
                    "sl_pct": 1.0,
                    "tp_pct": 2.0,
                },
            )
            assert created.status_code == 200

        listed = await client.get("/api/v1/live/auto-trade/configs")
        assert listed.status_code == 200
        body = listed.json()
        assert len(body["configs"]) == 2
        listed_accounts = {item["account_id"] for item in body["configs"]}
        assert listed_accounts == {account1_id, account2_id}
        assert body["active_account_id"] in listed_accounts
        assert body["active_config"] is not None
        assert body["active_config"]["account_id"] == body["active_account_id"]
        assert body["active_config"]["profile_id"] in {profile1_id, profile2_id}
        assert body["active_config"]["position_size_usdt"] == 100.0

        play_response = await client.post(f"/api/v1/live/auto-trade/play?account_id={account2_id}")
        assert play_response.status_code == 200

        listed_after_play = await client.get("/api/v1/live/auto-trade/configs")
        assert listed_after_play.status_code == 200
        body_after_play = listed_after_play.json()
        assert body_after_play["active_account_id"] == account2_id
        assert body_after_play["active_config"] is not None
        assert body_after_play["active_config"]["account_id"] == account2_id
        assert body_after_play["active_config"]["is_running"] is True


async def test_auto_trade_state_and_positions_endpoints(
    auto_trade_endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    profile_id, account_id = await _seed_profile_and_account(auto_trade_endpoints_db)
    position_id = await _seed_open_auto_trade_position(
        auto_trade_endpoints_db,
        profile_id=profile_id,
        account_id=account_id,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        state = await client.get("/api/v1/live/auto-trade/state")
        assert state.status_code == 200
        state_body = state.json()
        assert state_body["config"]["account_id"] == account_id

        positions = await client.get("/api/v1/live/auto-trade/positions?status=open")
        assert positions.status_code == 200
        positions_body = positions.json()
        assert positions_body["summary"]["total_positions"] == 1
        assert positions_body["summary"]["open_positions"] == 1
        assert positions_body["summary"]["closed_positions"] == 0
        assert len(positions_body["positions"]) == 1
        first = positions_body["positions"][0]
        assert first["position"]["id"] == position_id
        assert first["pnl"]["position_id"] == position_id


async def test_auto_trade_play_returns_404_without_config() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/live/auto-trade/play")
    assert response.status_code == 404


async def test_auto_trade_config_validates_profile_and_account_ownership(
    auto_trade_endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    own_profile_id, own_account_id = await _seed_profile_and_account(auto_trade_endpoints_db)
    foreign_profile_id, foreign_account_id = await _seed_foreign_profile_and_account(
        auto_trade_endpoints_db
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bad_profile = await client.put(
            "/api/v1/live/auto-trade/config",
            json={
                "enabled": True,
                "profile_id": foreign_profile_id,
                "account_id": own_account_id,
                "position_size_usdt": 100.0,
                "leverage": 1,
                "min_confidence_pct": 62.0,
                "fast_close_confidence_pct": 80.0,
                "confirm_reports_required": 2,
                "risk_mode": "1:2",
                "sl_pct": 1.0,
                "tp_pct": 2.0,
            },
        )
        assert bad_profile.status_code == 404

        bad_account = await client.put(
            "/api/v1/live/auto-trade/config",
            json={
                "enabled": True,
                "profile_id": own_profile_id,
                "account_id": foreign_account_id,
                "position_size_usdt": 100.0,
                "leverage": 1,
                "min_confidence_pct": 62.0,
                "fast_close_confidence_pct": 80.0,
                "confirm_reports_required": 2,
                "risk_mode": "1:2",
                "sl_pct": 1.0,
                "tp_pct": 2.0,
            },
        )
        assert bad_account.status_code == 404


async def test_auto_trade_config_rejects_profile_account_change_while_running(
    auto_trade_endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    profile_id, account_id = await _seed_profile_and_account(auto_trade_endpoints_db)
    profile2_id, account2_id = await _seed_second_profile_and_account_for_user1(
        auto_trade_endpoints_db
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_response = await client.put(
            "/api/v1/live/auto-trade/config",
            json={
                "enabled": True,
                "profile_id": profile_id,
                "account_id": account_id,
                "position_size_usdt": 100.0,
                "leverage": 1,
                "min_confidence_pct": 62.0,
                "fast_close_confidence_pct": 80.0,
                "confirm_reports_required": 2,
                "risk_mode": "1:2",
                "sl_pct": 1.0,
                "tp_pct": 2.0,
            },
        )
        assert create_response.status_code == 200

        play_response = await client.post("/api/v1/live/auto-trade/play")
        assert play_response.status_code == 200

        update_response = await client.put(
            "/api/v1/live/auto-trade/config",
            json={
                "enabled": True,
                "profile_id": profile2_id,
                "account_id": account2_id,
                "position_size_usdt": 100.0,
                "leverage": 1,
                "min_confidence_pct": 62.0,
                "fast_close_confidence_pct": 80.0,
                "confirm_reports_required": 2,
                "risk_mode": "1:2",
                "sl_pct": 1.0,
                "tp_pct": 2.0,
            },
        )
        assert update_response.status_code == 422


async def test_auto_trade_endpoints_require_account_scope_when_multiple_configs_exist(
    auto_trade_endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    profile_id, account_id = await _seed_profile_and_account(auto_trade_endpoints_db)
    profile2_id, account2_id = await _seed_second_profile_and_account_for_user1(
        auto_trade_endpoints_db
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for cfg_profile_id, cfg_account_id in (
            (profile_id, account_id),
            (profile2_id, account2_id),
        ):
            created = await client.put(
                "/api/v1/live/auto-trade/config",
                json={
                    "enabled": True,
                    "profile_id": cfg_profile_id,
                    "account_id": cfg_account_id,
                    "position_size_usdt": 100.0,
                    "leverage": 1,
                    "min_confidence_pct": 62.0,
                    "fast_close_confidence_pct": 80.0,
                    "confirm_reports_required": 2,
                    "risk_mode": "1:2",
                    "sl_pct": 1.0,
                    "tp_pct": 2.0,
                },
            )
            assert created.status_code == 200

        ambiguous = await client.get("/api/v1/live/auto-trade/config")
        assert ambiguous.status_code == 422
        assert "Provide account_id" in ambiguous.json()["detail"]

        state_ambiguous = await client.get("/api/v1/live/auto-trade/state")
        assert state_ambiguous.status_code == 422
        assert "Provide account_id" in state_ambiguous.json()["detail"]

        positions_ambiguous = await client.get("/api/v1/live/auto-trade/positions")
        assert positions_ambiguous.status_code == 422
        assert "Provide account_id" in positions_ambiguous.json()["detail"]

        events_ambiguous = await client.get("/api/v1/live/auto-trade/events")
        assert events_ambiguous.status_code == 422
        assert "Provide account_id" in events_ambiguous.json()["detail"]


async def test_auto_trade_play_stop_are_scoped_by_account(
    auto_trade_endpoints_db: async_sessionmaker[AsyncSession],
) -> None:
    profile_id, account_id = await _seed_profile_and_account(auto_trade_endpoints_db)
    profile2_id, account2_id = await _seed_second_profile_and_account_for_user1(
        auto_trade_endpoints_db
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for cfg_profile_id, cfg_account_id in (
            (profile_id, account_id),
            (profile2_id, account2_id),
        ):
            created = await client.put(
                "/api/v1/live/auto-trade/config",
                json={
                    "enabled": True,
                    "profile_id": cfg_profile_id,
                    "account_id": cfg_account_id,
                    "position_size_usdt": 100.0,
                    "leverage": 1,
                    "min_confidence_pct": 62.0,
                    "fast_close_confidence_pct": 80.0,
                    "confirm_reports_required": 2,
                    "risk_mode": "1:2",
                    "sl_pct": 1.0,
                    "tp_pct": 2.0,
                },
            )
            assert created.status_code == 200

        play_first = await client.post(f"/api/v1/live/auto-trade/play?account_id={account_id}")
        assert play_first.status_code == 200
        assert play_first.json()["config"]["account_id"] == account_id
        assert play_first.json()["config"]["is_running"] is True

        state_first = await client.get(f"/api/v1/live/auto-trade/state?account_id={account_id}")
        assert state_first.status_code == 200
        assert state_first.json()["config"]["is_running"] is True

        state_second = await client.get(f"/api/v1/live/auto-trade/state?account_id={account2_id}")
        assert state_second.status_code == 200
        assert state_second.json()["config"]["is_running"] is False

        play_second = await client.post(
            f"/api/v1/live/auto-trade/play?account_id={account2_id}"
        )
        assert play_second.status_code == 200
        assert play_second.json()["config"]["is_running"] is True

        stop_first = await client.post(f"/api/v1/live/auto-trade/stop?account_id={account_id}")
        assert stop_first.status_code == 200
        assert stop_first.json()["config"]["is_running"] is False

        still_running_second = await client.get(
            f"/api/v1/live/auto-trade/state?account_id={account2_id}"
        )
        assert still_running_second.status_code == 200
        assert still_running_second.json()["config"]["is_running"] is True
