from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.db.session import get_db_session
from app.main import app
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.live_paper_profile import LivePaperProfile
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.strategy import Strategy
from app.models.user import User


@pytest.fixture
async def admin_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "admin_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db_session(admin_db: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with admin_db() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session
    yield
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture
def set_current_user() -> Iterator[Callable[[User], None]]:
    def _set(user: User) -> None:
        async def _fake_current_user() -> User:
            return user

        app.dependency_overrides[get_current_user] = _fake_current_user

    yield _set
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_admin_runtime_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        admin_user = User(
            id=1,
            email="admin@example.com",
            hashed_password="x",
            is_active=True,
            is_admin=True,
        )
        trader_user = User(
            id=2,
            email="trader@example.com",
            hashed_password="x",
            is_active=True,
            is_admin=False,
        )
        session.add_all([admin_user, trader_user])

        admin_strategy = Strategy(
            user_id=1,
            name="Admin strategy",
            strategy_type="builder_vwap",
            version="1.0.0",
            description=None,
            is_active=True,
            config={"enabled": ["VWAP"]},
        )
        trader_strategy_active = Strategy(
            user_id=2,
            name="Trader strategy active",
            strategy_type="builder_vwap",
            version="1.0.0",
            description=None,
            is_active=True,
            config={"enabled": ["VWAP", "MACD"]},
        )
        trader_strategy_inactive = Strategy(
            user_id=2,
            name="Trader strategy inactive",
            strategy_type="atr_order_block",
            version="1.0.0",
            description="archived",
            is_active=False,
            config={"ema_period": 50},
        )
        session.add_all([admin_strategy, trader_strategy_active, trader_strategy_inactive])

        profile = PersonalAnalysisProfile(
            user_id=2,
            symbol="BTCUSDT",
            query_prompt=None,
            agents={"twitterSentiment": True},
            agent_weights={"twitterSentiment": 1.0},
            interval_minutes=60,
            is_active=True,
            next_run_at=now,
            last_triggered_at=None,
            last_completed_at=None,
        )
        account = ExchangeCredential(
            user_id=2,
            exchange_name="bybit",
            account_label="main",
            mode="demo",
            encrypted_api_key="k",
            encrypted_api_secret="s",
            encrypted_passphrase=None,
        )
        session.add_all([profile, account])
        await session.flush()

        auto_trade_config = AutoTradeConfig(
            user_id=2,
            profile_id=profile.id,
            account_id=account.id,
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
            last_started_at=now,
            last_stopped_at=None,
        )
        session.add(auto_trade_config)
        await session.flush()

        open_position = AutoTradePosition(
            user_id=2,
            config_id=auto_trade_config.id,
            profile_id=profile.id,
            account_id=account.id,
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
            opened_at=now,
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
        closed_position = AutoTradePosition(
            user_id=2,
            config_id=auto_trade_config.id,
            profile_id=profile.id,
            account_id=account.id,
            symbol="BTC/USDT:USDT",
            side="SHORT",
            status="closed",
            entry_price=105.0,
            quantity=1.0,
            position_size_usdt=100.0,
            leverage=2,
            tp_price=102.9,
            sl_price=106.05,
            entry_confidence_pct=71.0,
            opened_at=now,
            closed_at=now,
            close_reason="tp",
            close_price=102.9,
            open_order_id="ord-open-2",
            close_order_id="ord-close-2",
            open_history_id=None,
            close_history_id=None,
            raw_open_order={},
            raw_close_order={},
        )
        session.add_all([open_position, closed_position])

        live_paper_profile = LivePaperProfile(
            user_id=2,
            strategy_id=trader_strategy_active.id,
            strategy_revision=1,
            is_running=True,
            total_balance_usdt=2000.0,
            per_trade_usdt=100.0,
            last_processed_at=now,
            last_poll_at=now,
        )
        session.add(live_paper_profile)
        await session.commit()


async def test_admin_runtime_endpoint_forbidden_for_non_admin(
    set_current_user: Callable[[User], None],
) -> None:
    set_current_user(
        User(
            id=2,
            email="trader@example.com",
            hashed_password="x",
            is_active=True,
            is_admin=False,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/runtime")
    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required."


async def test_admin_runtime_endpoint_returns_full_snapshot(
    admin_db: async_sessionmaker[AsyncSession],
    set_current_user: Callable[[User], None],
) -> None:
    await _seed_admin_runtime_data(admin_db)
    set_current_user(
        User(
            id=1,
            email="admin@example.com",
            hashed_password="x",
            is_active=True,
            is_admin=True,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/runtime")
    assert response.status_code == 200
    body = response.json()
    assert body["page"] == {
        "users_limit": 50,
        "after_user_id": None,
        "next_after_user_id": None,
        "has_more": False,
    }
    assert body["summary"] == {
        "total_users": 2,
        "active_users": 2,
        "admin_users": 1,
        "total_strategies": 3,
        "active_strategies": 2,
        "total_auto_trade_configs": 1,
        "running_auto_trade_configs": 1,
        "total_auto_trade_positions": 2,
        "open_auto_trade_positions": 1,
        "running_live_paper_profiles": 1,
    }

    users = body["users"]
    assert len(users) == 2
    trader_snapshot = next(item for item in users if item["user"]["id"] == 2)
    assert trader_snapshot["stats"] == {
        "total_strategies": 2,
        "active_strategies": 1,
        "auto_trade_configs": 1,
        "running_auto_trade_configs": 1,
        "auto_trade_positions": 2,
        "open_auto_trade_positions": 1,
        "live_paper_running": True,
    }
    assert trader_snapshot["strategies_truncated"] is False
    assert trader_snapshot["auto_trade_configs_truncated"] is False
    assert trader_snapshot["auto_trade_positions_truncated"] is False
    assert len(trader_snapshot["strategies"]) == 2
    assert len(trader_snapshot["auto_trade_configs"]) == 1
    assert len(trader_snapshot["auto_trade_positions"]) == 2
    assert trader_snapshot["live_paper_profile"]["is_running"] is True


async def test_admin_runtime_endpoint_filters_open_positions(
    admin_db: async_sessionmaker[AsyncSession],
    set_current_user: Callable[[User], None],
) -> None:
    await _seed_admin_runtime_data(admin_db)
    set_current_user(
        User(
            id=1,
            email="admin@example.com",
            hashed_password="x",
            is_active=True,
            is_admin=True,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/runtime?positions_status=open")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total_auto_trade_positions"] == 1
    assert body["summary"]["open_auto_trade_positions"] == 1
    trader_snapshot = next(item for item in body["users"] if item["user"]["id"] == 2)
    assert len(trader_snapshot["auto_trade_positions"]) == 1
    assert trader_snapshot["auto_trade_positions"][0]["status"] == "open"


async def test_admin_runtime_endpoint_supports_cursor_pagination(
    admin_db: async_sessionmaker[AsyncSession],
    set_current_user: Callable[[User], None],
) -> None:
    await _seed_admin_runtime_data(admin_db)
    set_current_user(
        User(
            id=1,
            email="admin@example.com",
            hashed_password="x",
            is_active=True,
            is_admin=True,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/v1/admin/runtime?users_limit=1")
        assert first.status_code == 200
        first_body = first.json()
        assert len(first_body["users"]) == 1
        assert first_body["page"]["has_more"] is True
        next_cursor = first_body["page"]["next_after_user_id"]
        assert isinstance(next_cursor, int)

        second = await client.get(
            f"/api/v1/admin/runtime?users_limit=1&after_user_id={next_cursor}"
        )
        assert second.status_code == 200
        second_body = second.json()
        assert len(second_body["users"]) == 1
        assert second_body["page"]["has_more"] is False
        assert second_body["page"]["next_after_user_id"] is None
        assert second_body["users"][0]["user"]["id"] != first_body["users"][0]["user"]["id"]


async def test_admin_runtime_endpoint_applies_nested_limits(
    admin_db: async_sessionmaker[AsyncSession],
    set_current_user: Callable[[User], None],
) -> None:
    await _seed_admin_runtime_data(admin_db)
    set_current_user(
        User(
            id=1,
            email="admin@example.com",
            hashed_password="x",
            is_active=True,
            is_admin=True,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/admin/runtime?strategies_limit_per_user=1&positions_limit_per_user=1"
        )
    assert response.status_code == 200
    body = response.json()
    trader_snapshot = next(item for item in body["users"] if item["user"]["id"] == 2)
    assert len(trader_snapshot["strategies"]) == 1
    assert len(trader_snapshot["auto_trade_positions"]) == 1
    assert trader_snapshot["strategies_truncated"] is True
    assert trader_snapshot["auto_trade_positions_truncated"] is True


async def test_admin_runtime_endpoint_can_skip_heavy_details(
    admin_db: async_sessionmaker[AsyncSession],
    set_current_user: Callable[[User], None],
) -> None:
    await _seed_admin_runtime_data(admin_db)
    set_current_user(
        User(
            id=1,
            email="admin@example.com",
            hashed_password="x",
            is_active=True,
            is_admin=True,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/runtime?include_details=false")
    assert response.status_code == 200
    body = response.json()
    trader_snapshot = next(item for item in body["users"] if item["user"]["id"] == 2)
    assert trader_snapshot["strategies"] == []
    assert trader_snapshot["auto_trade_configs"] == []
    assert trader_snapshot["auto_trade_positions"] == []
    assert trader_snapshot["live_paper_profile"] is None
    assert trader_snapshot["stats"]["total_strategies"] == 2
