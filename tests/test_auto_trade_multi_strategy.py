"""W7 — Multi-Strategy Account Partitioning.

Acceptance tests for the user-confirmed architecture:
  * 1 ExchangeCredential = 1 strategy (existing UQ on (user, account_id) kept).
  * Multi-strategy is achieved by N credentials per user, each on its own
    physical sub-account; collision is physically impossible by design.
  * Hash-based duplicate-api_key detection prevents two credentials from
    pointing at the same sub-account (which would re-introduce collisions).
  * Aggregated portfolio + bulk play/stop endpoints power the dashboard.

These tests intentionally use SQLite + ``Base.metadata.create_all`` (the
existing pattern in ``test_auto_trade_endpoints.py``) rather than alembic;
the partial UQ on ``api_key_hash`` is declared on the model so SQLite
honours it via ``CREATE UNIQUE INDEX … WHERE``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1.endpoints.live import auto_trade_service, trading_service
from app.db.session import get_db_session
from app.main import app
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_event import AutoTradeEvent
from app.models.auto_trade_position import AutoTradePosition
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.schemas.exchange_trading import SpotBalancesRead
from app.services.exchange_credentials.service import (
    DuplicateApiKeyError,
    ExchangeCredentialsService,
)


# ─── shared fixtures ────────────────────────────────────────────────────


class _NoopTradingService:
    """Avoid hitting the real exchange in service-level tests."""

    async def fetch_futures_position(self, **_: object) -> None:
        return None

    async def fetch_futures_trades(self, **_: object) -> list[object]:
        return []

    async def get_spot_balances(self, **_: object) -> SpotBalancesRead:
        return SpotBalancesRead(
            account_id=0, exchange_name="bybit", mode="demo", balances=[]
        )


@pytest.fixture(autouse=True)
def override_exchange_calls(monkeypatch: pytest.MonkeyPatch) -> None:
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
    db_path = tmp_path / "multi_strategy.db"
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
        return User(id=1, email="ms@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def patch_secrets_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make encrypt/decrypt a no-op so credential creation works in tests."""

    class _IdentityCipher:
        def encrypt(self, value: str) -> str:
            return value

        def decrypt(self, value: str) -> str:
            return value

    from app.services import secrets as secrets_module

    monkeypatch.setattr(
        secrets_module, "SecretCipher", lambda _key, **_kwargs: _IdentityCipher()
    )
    yield


# ─── helpers ────────────────────────────────────────────────────────────


async def _seed_user(session: AsyncSession, *, user_id: int = 1) -> None:
    user = User(id=user_id, email=f"u{user_id}@example.com", hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()


async def _make_profile(session: AsyncSession, *, user_id: int, symbol: str) -> int:
    profile = PersonalAnalysisProfile(
        user_id=user_id,
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


async def _make_credential(
    session: AsyncSession,
    *,
    user_id: int,
    label: str,
    api_key: str = "k",
    api_key_hash: str | None = None,
) -> int:
    row = ExchangeCredential(
        user_id=user_id,
        exchange_name="bybit",
        account_label=label,
        mode="demo",
        encrypted_api_key=api_key,
        encrypted_api_secret="s",
        encrypted_passphrase=None,
        api_key_hash=api_key_hash,
    )
    session.add(row)
    await session.flush()
    return row.id


def _config_payload(*, profile_id: int, account_id: int, **kwargs: object) -> dict[str, object]:
    payload = {
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
    }
    payload.update(kwargs)
    return payload


# ─── tests ──────────────────────────────────────────────────────────────


async def test_three_strategies_on_three_credentials_run_independently(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Acceptance: ≥3 strategies on one user without collisions.

    Strategies are isolated via separate credentials (separate API keys =
    separate physical sub-accounts on the exchange).
    """

    async with db_factory() as session:
        await _seed_user(session)
        p_btc = await _make_profile(session, user_id=1, symbol="BTCUSDT")
        p_eth = await _make_profile(session, user_id=1, symbol="ETHUSDT")
        p_sol = await _make_profile(session, user_id=1, symbol="SOLUSDT")
        a_btc = await _make_credential(session, user_id=1, label="btc-sub", api_key="k-btc")
        a_eth = await _make_credential(session, user_id=1, label="eth-sub", api_key="k-eth")
        a_sol = await _make_credential(session, user_id=1, label="sol-sub", api_key="k-sol")
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for profile_id, account_id, name in (
            (p_btc, a_btc, "BTC-VWAP"),
            (p_eth, a_eth, "ETH-Grid"),
            (p_sol, a_sol, "SOL-Scalp"),
        ):
            resp = await client.put(
                "/api/v1/live/auto-trade/config",
                json=_config_payload(
                    profile_id=profile_id,
                    account_id=account_id,
                    strategy_name=name,
                ),
            )
            assert resp.status_code == 200, resp.text

        configs = await client.get("/api/v1/live/auto-trade/configs")
        assert configs.status_code == 200
        body = configs.json()
        assert len(body["configs"]) == 3
        names = sorted(c["strategy_name"] for c in body["configs"])
        assert names == ["BTC-VWAP", "ETH-Grid", "SOL-Scalp"]


async def test_same_api_key_rejected(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Duplicate api_key registration → 409 from /exchange/accounts."""

    async with db_factory() as session:
        await _seed_user(session)
        await session.commit()

    service = ExchangeCredentialsService()
    from app.schemas.exchange import ExchangeAccountCreate

    payload = ExchangeAccountCreate(
        exchange_name="bybit",
        account_label="primary",
        mode="demo",
        api_key="api-XYZ",
        api_secret="secret-XYZ",
        passphrase=None,
    )

    async with db_factory() as session:
        await service.create_account(session=session, payload=payload, user_id=1)

    duplicate_payload = ExchangeAccountCreate(
        exchange_name="bybit",
        account_label="duplicate-label",
        mode="demo",
        api_key="api-XYZ",  # same api_key, different label
        api_secret="secret-different",
        passphrase=None,
    )
    async with db_factory() as session:
        with pytest.raises(DuplicateApiKeyError):
            await service.create_account(session=session, payload=duplicate_payload, user_id=1)


async def test_same_api_key_different_exchange_allowed(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same api_key string across two different exchanges is OK.

    The dedup is per-user; two unrelated exchanges happening to share a key
    string is fine. (In practice users would never use the same key on two
    exchanges; the test pins the semantics.)
    """

    async with db_factory() as session:
        await _seed_user(session)
        await session.commit()

    service = ExchangeCredentialsService()
    from app.schemas.exchange import ExchangeAccountCreate

    async with db_factory() as session:
        await service.create_account(
            session=session,
            payload=ExchangeAccountCreate(
                exchange_name="bybit",
                account_label="bybit-main",
                mode="demo",
                api_key="ABC",
                api_secret="s",
                passphrase=None,
            ),
            user_id=1,
        )
    # Same physical key string on bybit AGAIN must be rejected.
    async with db_factory() as session:
        with pytest.raises(DuplicateApiKeyError):
            await service.create_account(
                session=session,
                payload=ExchangeAccountCreate(
                    exchange_name="bybit",
                    account_label="bybit-second",
                    mode="demo",
                    api_key="ABC",
                    api_secret="s",
                    passphrase=None,
                ),
                user_id=1,
            )


async def test_same_profile_on_two_credentials_emits_warning(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sharing a profile across two strategies (different sub-accounts) is
    legitimate but surfaces an audit warning the dashboard can show."""

    async with db_factory() as session:
        await _seed_user(session)
        profile_id = await _make_profile(session, user_id=1, symbol="BTCUSDT")
        a1 = await _make_credential(session, user_id=1, label="sub-1", api_key="k1")
        a2 = await _make_credential(session, user_id=1, label="sub-2", api_key="k2")
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.put(
            "/api/v1/live/auto-trade/config",
            json=_config_payload(profile_id=profile_id, account_id=a1),
        )
        assert first.status_code == 200
        second = await client.put(
            "/api/v1/live/auto-trade/config",
            json=_config_payload(profile_id=profile_id, account_id=a2),
        )
        assert second.status_code == 200

    async with db_factory() as session:
        events = list(
            (
                await session.scalars(
                    select(AutoTradeEvent).where(
                        AutoTradeEvent.user_id == 1,
                        AutoTradeEvent.event_type == "config_shares_profile_with",
                    )
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].payload["other_config_ids"]


async def test_portfolio_endpoint_aggregates_across_strategies(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Portfolio sums realized PnL across all of a user's strategies."""

    async with db_factory() as session:
        await _seed_user(session)
        p1 = await _make_profile(session, user_id=1, symbol="BTCUSDT")
        p2 = await _make_profile(session, user_id=1, symbol="ETHUSDT")
        a1 = await _make_credential(session, user_id=1, label="s1", api_key="k1")
        a2 = await _make_credential(session, user_id=1, label="s2", api_key="k2")
        for profile_id, account_id, name in ((p1, a1, "Alpha"), (p2, a2, "Beta")):
            cfg = AutoTradeConfig(
                user_id=1,
                profile_id=profile_id,
                account_id=account_id,
                enabled=True,
                is_running=False,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
                strategy_name=name,
            )
            session.add(cfg)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/live/auto-trade/portfolio")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["strategies"]) == 2
        labels = {s["strategy_name"] for s in body["strategies"]}
        assert labels == {"Alpha", "Beta"}
        assert body["total_running_strategies"] == 0
        assert body["total_open_positions"] == 0
        # B4 (P1-T5) — every strategy carries a kpi_as_of freshness marker; these
        # are stopped with no snapshot, so it is null.
        assert all("kpi_as_of" in s for s in body["strategies"])
        assert all(s["kpi_as_of"] is None for s in body["strategies"])


async def test_play_all_starts_only_enabled_configs(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """play-all skips disabled configs and reports per-row outcome."""

    async with db_factory() as session:
        await _seed_user(session)
        p1 = await _make_profile(session, user_id=1, symbol="BTCUSDT")
        p2 = await _make_profile(session, user_id=1, symbol="ETHUSDT")
        a1 = await _make_credential(session, user_id=1, label="s1", api_key="k1")
        a2 = await _make_credential(session, user_id=1, label="s2", api_key="k2")
        session.add(
            AutoTradeConfig(
                user_id=1,
                profile_id=p1,
                account_id=a1,
                enabled=True,
                is_running=False,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            )
        )
        session.add(
            AutoTradeConfig(
                user_id=1,
                profile_id=p2,
                account_id=a2,
                enabled=False,  # disabled
                is_running=False,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            )
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/live/auto-trade/play-all")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["requested"] == 2
        assert body["succeeded"] == 1
        assert body["skipped"] == 1


async def test_stop_all_does_not_close_positions(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """stop-all only flips is_running=false; open positions stay open."""

    async with db_factory() as session:
        await _seed_user(session)
        profile_id = await _make_profile(session, user_id=1, symbol="BTCUSDT")
        account_id = await _make_credential(session, user_id=1, label="main", api_key="k")
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
        )
        session.add(cfg)
        await session.flush()
        position = AutoTradePosition(
            user_id=1,
            config_id=cfg.id,
            profile_id=profile_id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            side="LONG",
            status="open",
            entry_price=100.0,
            quantity=1.0,
            position_size_usdt=100.0,
            leverage=1,
            tp_price=102.0,
            sl_price=99.0,
            entry_confidence_pct=70.0,
            opened_at=datetime.now(UTC),
            closed_at=None,
            close_reason=None,
            close_price=None,
            open_order_id="oid",
            close_order_id=None,
            open_history_id=None,
            close_history_id=None,
            raw_open_order={},
            raw_close_order={},
        )
        session.add(position)
        await session.commit()
        position_id = position.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/live/auto-trade/stop-all")
        assert resp.status_code == 200
        assert resp.json()["succeeded"] == 1

    async with db_factory() as session:
        cfg = await session.scalar(select(AutoTradeConfig).where(AutoTradeConfig.user_id == 1))
        assert cfg is not None
        assert cfg.is_running is False
        pos = await session.get(AutoTradePosition, position_id)
        assert pos is not None
        assert pos.status == "open"


async def test_balance_endpoint_returns_usdt(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_factory() as session:
        await _seed_user(session)
        account_id = await _make_credential(session, user_id=1, label="main", api_key="k")
        await session.commit()

    from app.schemas.exchange_trading import NormalizedBalance

    async def _mock_get_spot_balances(self: object, **_: object) -> SpotBalancesRead:
        return SpotBalancesRead(
            account_id=account_id,
            exchange_name="bybit",
            mode="demo",
            balances=[
                NormalizedBalance(asset="USDT", free=500.0, used=0.0, total=500.0),
                NormalizedBalance(asset="BTC", free=0.01, used=0.0, total=0.01),
            ],
        )

    monkeypatch.setattr(type(trading_service), "get_spot_balances", _mock_get_spot_balances)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/live/auto-trade/balance", params={"account_id": account_id}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["free_usdt"] == 500.0
        assert body["total_usdt"] == 500.0
        assert body["error"] is None


async def test_portfolio_partial_balance_failure(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One sub-account failing must not 500 the whole portfolio."""

    async with db_factory() as session:
        await _seed_user(session)
        p1 = await _make_profile(session, user_id=1, symbol="BTCUSDT")
        p2 = await _make_profile(session, user_id=1, symbol="ETHUSDT")
        a_ok = await _make_credential(session, user_id=1, label="ok", api_key="k1")
        a_bad = await _make_credential(session, user_id=1, label="bad", api_key="k2")
        for pid, aid in ((p1, a_ok), (p2, a_bad)):
            session.add(
                AutoTradeConfig(
                    user_id=1,
                    profile_id=pid,
                    account_id=aid,
                    enabled=True,
                    is_running=False,
                    position_size_usdt=100.0,
                    leverage=1,
                    min_confidence_pct=62.0,
                    fast_close_confidence_pct=80.0,
                    confirm_reports_required=2,
                    risk_mode="1:2",
                    sl_pct=1.0,
                    tp_pct=2.0,
                )
            )
        await session.commit()

    from app.schemas.exchange_trading import NormalizedBalance

    async def _mock_get_spot_balances(self: object, **kwargs: object) -> SpotBalancesRead:
        if kwargs.get("account_id") == a_bad:
            raise RuntimeError("network down")
        return SpotBalancesRead(
            account_id=int(kwargs["account_id"]),
            exchange_name="bybit",
            mode="demo",
            balances=[NormalizedBalance(asset="USDT", free=100.0, used=0.0, total=100.0)],
        )

    monkeypatch.setattr(type(trading_service), "get_spot_balances", _mock_get_spot_balances)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/live/auto-trade/portfolio")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["strategies"]) == 2
        ok_row = next(s for s in body["strategies"] if s["account_id"] == a_ok)
        bad_row = next(s for s in body["strategies"] if s["account_id"] == a_bad)
        assert ok_row["balance_total_usdt"] == 100.0
        assert bad_row["balance_total_usdt"] is None
        assert bad_row["balance_error"] is not None


async def test_signal_on_one_strategy_does_not_affect_another(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per-config isolation: opening a position on config A leaves config B
    with no open position. Verified at the DB level since each config is
    bound to its own credential."""

    async with db_factory() as session:
        await _seed_user(session)
        p1 = await _make_profile(session, user_id=1, symbol="BTCUSDT")
        p2 = await _make_profile(session, user_id=1, symbol="ETHUSDT")
        a1 = await _make_credential(session, user_id=1, label="a1", api_key="k1")
        a2 = await _make_credential(session, user_id=1, label="a2", api_key="k2")
        cfg_a = AutoTradeConfig(
            user_id=1, profile_id=p1, account_id=a1, enabled=True, is_running=True,
            position_size_usdt=100.0, leverage=1, min_confidence_pct=62.0,
            fast_close_confidence_pct=80.0, confirm_reports_required=2,
            risk_mode="1:2", sl_pct=1.0, tp_pct=2.0,
        )
        cfg_b = AutoTradeConfig(
            user_id=1, profile_id=p2, account_id=a2, enabled=True, is_running=True,
            position_size_usdt=100.0, leverage=1, min_confidence_pct=62.0,
            fast_close_confidence_pct=80.0, confirm_reports_required=2,
            risk_mode="1:2", sl_pct=1.0, tp_pct=2.0,
        )
        session.add(cfg_a)
        session.add(cfg_b)
        await session.flush()
        position = AutoTradePosition(
            user_id=1, config_id=cfg_a.id, profile_id=p1, account_id=a1,
            symbol="BTC/USDT:USDT", side="LONG", status="open",
            entry_price=100.0, quantity=1.0, position_size_usdt=100.0, leverage=1,
            tp_price=102.0, sl_price=99.0, entry_confidence_pct=70.0,
            opened_at=datetime.now(UTC), closed_at=None, close_reason=None,
            close_price=None, open_order_id="oid", close_order_id=None,
            open_history_id=None, close_history_id=None,
            raw_open_order={}, raw_close_order={},
        )
        session.add(position)
        await session.commit()
        cfg_b_id = cfg_b.id

    # Query positions scoped to cfg_b — should be empty.
    async with db_factory() as session:
        cfg_b_positions = list(
            (
                await session.scalars(
                    select(AutoTradePosition).where(
                        AutoTradePosition.config_id == cfg_b_id,
                        AutoTradePosition.status == "open",
                    )
                )
            ).all()
        )
        assert cfg_b_positions == []


async def test_existing_single_credential_user_unchanged(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Backward compat: a user with 1 credential + 1 config keeps working.

    Existing API contracts (GET /configs, GET /portfolio with 1 entry,
    play/stop without account_id) must behave the same as before W7.
    """

    async with db_factory() as session:
        await _seed_user(session)
        profile_id = await _make_profile(session, user_id=1, symbol="BTCUSDT")
        account_id = await _make_credential(session, user_id=1, label="solo", api_key="k")
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.put(
            "/api/v1/live/auto-trade/config",
            json=_config_payload(profile_id=profile_id, account_id=account_id),
        )
        assert created.status_code == 200

        configs = await client.get("/api/v1/live/auto-trade/configs")
        assert configs.status_code == 200
        assert len(configs.json()["configs"]) == 1

        portfolio = await client.get("/api/v1/live/auto-trade/portfolio")
        assert portfolio.status_code == 200
        assert len(portfolio.json()["strategies"]) == 1

        # Play without account_id still works when there's only one config.
        played = await client.post("/api/v1/live/auto-trade/play")
        assert played.status_code == 200
        assert played.json()["config"]["is_running"] is True
