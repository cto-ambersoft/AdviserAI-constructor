"""W7 Asset Expansion — end-to-end ETH/USDT coverage.

The TZ acceptance criterion is "execution of trades on BTC/USDT and
ETH/USDT". The backend was already largely symbol-agnostic; these tests
pin that ETH-specific flows do not regress and validate the
config_id-aware endpoints introduced for multi-strategy partitioning.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
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
from app.services.auto_trade.signal import to_linear_perp_symbol


@pytest.fixture(autouse=True)
def override_exchange_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub adapter to keep tests offline."""

    class _NoopTradingService:
        async def fetch_futures_position(self, **_: object) -> None:
            return None

        async def fetch_futures_trades(self, **_: object) -> list[object]:
            return []

    monkeypatch.setattr(auto_trade_service, "_trading", _NoopTradingService())

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
async def db_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "eth_e2e.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db_session(db_factory: async_sessionmaker[AsyncSession]) -> None:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session
    yield
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture(autouse=True)
def override_current_user() -> None:
    async def _fake_current_user() -> User:
        return User(id=1, email="eth-e2e@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_user(session: AsyncSession) -> None:
    session.add(User(id=1, email="eth-e2e@example.com", hashed_password="x", is_active=True))
    await session.flush()


async def _make_profile(session: AsyncSession, *, symbol: str) -> int:
    profile = PersonalAnalysisProfile(
        user_id=1,
        symbol=symbol,
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
    return profile.id


async def _make_credential(session: AsyncSession, *, label: str, api_key: str) -> int:
    row = ExchangeCredential(
        user_id=1,
        exchange_name="bybit",
        account_label=label,
        mode="demo",
        encrypted_api_key=api_key,
        encrypted_api_secret="s",
        encrypted_passphrase=None,
        api_key_hash=None,
    )
    session.add(row)
    await session.flush()
    return row.id


def _config_payload(*, profile_id: int, account_id: int, **kw: object) -> dict[str, object]:
    return {
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
        **kw,
    }


# ─── tests ──────────────────────────────────────────────────────────────


async def test_personal_analysis_profile_accepts_eth_symbol(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PersonalAnalysisProfile.symbol is a free-text String(24); ETHUSDT fits."""

    async with db_factory() as session:
        await _seed_user(session)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/analysis/personal/profiles",
            json={
                "symbol": "ETHUSDT",
                "interval_minutes": 60,
                "is_active": True,
                "agents": {"twitterSentiment": True},
                "agent_weights": {"twitterSentiment": 1.0},
            },
        )
        assert resp.status_code in (200, 201), resp.text
        body = resp.json()
        assert body["symbol"] == "ETHUSDT"


def test_to_linear_perp_symbol_handles_eth_variants() -> None:
    """W7 acceptance — the symbol-normalisation function must understand
    every form the UI / external API may pass for ETH/USDT."""

    assert to_linear_perp_symbol("ETHUSDT") == "ETH/USDT:USDT"
    assert to_linear_perp_symbol("eth/usdt") == "ETH/USDT:USDT"
    assert to_linear_perp_symbol("ETH/USDT:USDT") == "ETH/USDT:USDT"
    assert to_linear_perp_symbol("ETHUSDC") == "ETH/USDC:USDC"


async def test_autotrade_upsert_config_accepts_eth_profile(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AutoTradeConfig upsert resolves an ETH PersonalAnalysisProfile and
    passes the profile.symbol through ``to_linear_perp_symbol`` without
    error (the upsert validates the symbol before saving)."""

    async with db_factory() as session:
        await _seed_user(session)
        profile_id = await _make_profile(session, symbol="ETHUSDT")
        account_id = await _make_credential(session, label="eth-sub", api_key="k-eth")
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/live/auto-trade/config",
            json=_config_payload(profile_id=profile_id, account_id=account_id, strategy_name="ETH-Grid"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["profile_id"] == profile_id
        assert body["strategy_name"] == "ETH-Grid"


async def test_autotrade_btc_and_eth_coexist_on_one_account(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """W7 architecture intent: one strategy = one credential. BTC and ETH
    on the SAME user therefore require two credentials. This test pins
    the two-credential setup; UI then distinguishes them via config_id.
    """

    async with db_factory() as session:
        await _seed_user(session)
        p_btc = await _make_profile(session, symbol="BTCUSDT")
        p_eth = await _make_profile(session, symbol="ETHUSDT")
        a_btc = await _make_credential(session, label="btc-sub", api_key="k-btc")
        a_eth = await _make_credential(session, label="eth-sub", api_key="k-eth")
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.put(
            "/api/v1/live/auto-trade/config",
            json=_config_payload(profile_id=p_btc, account_id=a_btc, strategy_name="BTC-VWAP"),
        )
        assert first.status_code == 200
        second = await client.put(
            "/api/v1/live/auto-trade/config",
            json=_config_payload(profile_id=p_eth, account_id=a_eth, strategy_name="ETH-Grid"),
        )
        assert second.status_code == 200

        configs = await client.get("/api/v1/live/auto-trade/configs")
        assert configs.status_code == 200
        body = configs.json()
        names = sorted(c["strategy_name"] for c in body["configs"])
        assert names == ["BTC-VWAP", "ETH-Grid"]


async def test_autotrade_position_persists_eth_symbol(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end persistence sanity: an ETH position lands in
    ``auto_trade_positions`` with the linear-perp symbol and is returnable
    via the config_id-scoped endpoint."""

    async with db_factory() as session:
        await _seed_user(session)
        profile_id = await _make_profile(session, symbol="ETHUSDT")
        account_id = await _make_credential(session, label="eth-sub", api_key="k-eth")
        cfg = AutoTradeConfig(
            user_id=1,
            profile_id=profile_id,
            account_id=account_id,
            enabled=True,
            is_running=True,
            position_size_usdt=100.0,
            leverage=1,
            min_confidence_pct=62.0,
            fast_close_confidence_pct=80.0,
            confirm_reports_required=2,
            risk_mode="1:2",
            sl_pct=1.0,
            tp_pct=2.0,
            strategy_name="ETH-Grid",
        )
        session.add(cfg)
        await session.flush()
        position = AutoTradePosition(
            user_id=1,
            config_id=cfg.id,
            profile_id=profile_id,
            account_id=account_id,
            symbol="ETH/USDT:USDT",
            side="LONG",
            status="open",
            entry_price=3500.0,
            quantity=0.0286,
            position_size_usdt=100.0,
            leverage=1,
            tp_price=3570.0,
            sl_price=3465.0,
            entry_confidence_pct=70.0,
            opened_at=datetime.now(UTC),
            closed_at=None,
            close_reason=None,
            close_price=None,
            open_order_id="eth-open",
            close_order_id=None,
            open_history_id=None,
            close_history_id=None,
            raw_open_order={},
            raw_close_order={},
        )
        session.add(position)
        await session.commit()
        config_id = cfg.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/live/auto-trade/positions",
            params={"config_id": config_id, "status": "open"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["positions"]) == 1
        assert body["positions"][0]["position"]["symbol"] == "ETH/USDT:USDT"


async def test_events_endpoint_isolates_btc_and_eth_by_config_id(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Critical for the UI fix: with two configs (BTC + ETH) on a user,
    the events endpoint must scope strictly by ``config_id``. Passing
    ``account_id`` alone could (under hypothetical multi-config-per-
    account scenarios) leak the other strategy's audit log."""

    from app.models.auto_trade_event import AutoTradeEvent

    async with db_factory() as session:
        await _seed_user(session)
        p_btc = await _make_profile(session, symbol="BTCUSDT")
        p_eth = await _make_profile(session, symbol="ETHUSDT")
        a_btc = await _make_credential(session, label="btc-sub", api_key="k-btc")
        a_eth = await _make_credential(session, label="eth-sub", api_key="k-eth")
        cfg_btc = AutoTradeConfig(
            user_id=1, profile_id=p_btc, account_id=a_btc, enabled=True, is_running=False,
            position_size_usdt=100.0, leverage=1, min_confidence_pct=62.0,
            fast_close_confidence_pct=80.0, confirm_reports_required=2,
            risk_mode="1:2", sl_pct=1.0, tp_pct=2.0,
        )
        cfg_eth = AutoTradeConfig(
            user_id=1, profile_id=p_eth, account_id=a_eth, enabled=True, is_running=False,
            position_size_usdt=100.0, leverage=1, min_confidence_pct=62.0,
            fast_close_confidence_pct=80.0, confirm_reports_required=2,
            risk_mode="1:2", sl_pct=1.0, tp_pct=2.0,
        )
        session.add_all([cfg_btc, cfg_eth])
        await session.flush()
        session.add_all(
            [
                AutoTradeEvent(
                    user_id=1, config_id=cfg_btc.id, profile_id=p_btc,
                    history_id=None, position_id=None,
                    event_type="signal_skipped_neutral_trend",
                    level="info", message="BTC neutral.", payload={},
                ),
                AutoTradeEvent(
                    user_id=1, config_id=cfg_eth.id, profile_id=p_eth,
                    history_id=None, position_id=None,
                    event_type="signal_skipped_neutral_trend",
                    level="info", message="ETH neutral.", payload={},
                ),
            ]
        )
        await session.commit()
        btc_cfg_id = cfg_btc.id
        eth_cfg_id = cfg_eth.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        btc_events = await client.get(
            "/api/v1/live/auto-trade/events", params={"config_id": btc_cfg_id}
        )
        eth_events = await client.get(
            "/api/v1/live/auto-trade/events", params={"config_id": eth_cfg_id}
        )
        assert btc_events.status_code == 200, btc_events.text
        assert eth_events.status_code == 200, eth_events.text
        btc_msgs = [e["message"] for e in btc_events.json()["events"]]
        eth_msgs = [e["message"] for e in eth_events.json()["events"]]
        assert "BTC neutral." in btc_msgs
        assert "ETH neutral." not in btc_msgs
        assert "ETH neutral." in eth_msgs
        assert "BTC neutral." not in eth_msgs


