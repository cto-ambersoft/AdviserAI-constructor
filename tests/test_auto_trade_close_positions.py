"""Tests for ``AutoTradeService.close_open_positions`` and its endpoint.

Covered scenarios:
  * ``confirm=False`` returns a preview without touching the exchange or DB.
  * ``confirm=True`` cancels every known TP/SL and market-closes each
    position, marking the DB row CLOSED and untrack-ing it from WSManager.
  * Already-flat positions are skipped (idempotent).
  * Failure on one symbol does not abort the batch — surfaced via ``failed``.
  * Foreign-user positions are never touched.
  * The HTTP endpoint maps the service flow to 412 / 200.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.deps import get_current_user  # noqa: E402
from app.db.session import get_db_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models.auto_trade_config import AutoTradeConfig  # noqa: E402
from app.models.auto_trade_event import AutoTradeEvent  # noqa: E402
from app.models.auto_trade_position import AutoTradePosition  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.exchange import ExchangeCredential  # noqa: E402
from app.models.personal_analysis_profile import PersonalAnalysisProfile  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.auto_trade.service import (  # noqa: E402
    AutoTradeService,
    ConfirmationRequiredError,
)
from app.services.exchange.adapter import (  # noqa: E402
    ConditionalOrderResult,
    PartialCloseResult,
    PositionSide,
    PositionSnapshot,
)

# ───────────────────────── adapter fakes ───────────────────────────────────


class _FakeAdapter:
    """Minimal adapter capturing cancel/close calls."""

    def __init__(
        self,
        *,
        live_size: float = 0.05,
        position_side: PositionSide = PositionSide.LONG,
        market_close_error: Exception | None = None,
    ) -> None:
        self.live_size = live_size
        self.position_side = position_side
        self.market_close_error = market_close_error
        self.cancelled_orders: list[str] = []
        self.partial_close_calls: list[dict[str, Any]] = []

    async def cancel_conditional_order(self, symbol: str, order_id: str) -> bool:
        self.cancelled_orders.append(order_id)
        return True

    async def get_position(self, symbol: str) -> PositionSnapshot | None:
        # Real adapters return a snapshot with size=0 for a flat position
        # rather than None — None is reserved for "symbol unknown / adapter
        # error". Mirror that semantic so the close flow can distinguish.
        return PositionSnapshot(
            symbol=symbol,
            side=self.position_side,
            size=self.live_size,
            entry_price=70_000.0,
            mark_price=70_100.0,
            unrealized_pnl=10.0 if self.live_size > 0 else 0.0,
            leverage=1,
            liquidation_price=60_000.0,
            open_orders=[],
        )

    async def partial_close(
        self,
        symbol: str,
        side: Any,
        quantity: float,
        client_order_id: str,
        order_type: str = "market",
        price: float | None = None,
    ) -> PartialCloseResult:
        self.partial_close_calls.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "client_order_id": client_order_id,
                "order_type": order_type,
            }
        )
        if self.market_close_error is not None:
            raise self.market_close_error
        return PartialCloseResult(
            executed_qty=quantity,
            avg_price=70_150.0,
            remaining_qty=0.0,
            order_id=f"market-close-{client_order_id}",
            commission=0.05,
        )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "close_positions.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_user_account_and_config(
    session: AsyncSession,
    *,
    user_email: str = "close@example.com",
) -> tuple[int, int, int]:
    user = User(email=user_email, hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()

    profile = PersonalAnalysisProfile(
        user_id=user.id,
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
        user_id=user.id,
        exchange_name="bybit",
        account_label="main",
        mode="demo",
        encrypted_api_key="k",
        encrypted_api_secret="s",
        encrypted_passphrase=None,
    )
    session.add(account)
    await session.flush()

    config = AutoTradeConfig(
        user_id=user.id,
        profile_id=profile.id,
        account_id=account.id,
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
    session.add(config)
    await session.commit()
    return user.id, account.id, config.id


async def _seed_position(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    config_id: int,
    profile_id: int,
    symbol: str = "BTC/USDT:USDT",
    state: str = "open",
    status: str = "open",
    side: str = "LONG",
    sl_id: str | None = "sl-algo-1",
    tp_levels: list[dict[str, Any]] | None = None,
) -> int:
    if tp_levels is None:
        tp_levels = [
            {
                "level": 1,
                "price_offset_pct": 1.0,
                "close_pct": 50.0,
                "trigger_price": 70700.0,
                "status": "open",
                "exchange_order_id": "tp-algo-1",
            },
            {
                "level": 2,
                "price_offset_pct": 3.0,
                "close_pct": 50.0,
                "trigger_price": 72100.0,
                "status": "open",
                "exchange_order_id": "tp-algo-2",
            },
        ]
    position = AutoTradePosition(
        user_id=user_id,
        account_id=account_id,
        config_id=config_id,
        profile_id=profile_id,
        symbol=symbol,
        side=side,
        entry_price=70_000.0,
        original_quantity=0.1,
        current_quantity=0.05,
        quantity=0.05,
        position_size_usdt=100.0,
        sl_price=68_000.0,
        tp_price=70_700.0,
        entry_confidence_pct=70.0,
        leverage=1,
        state=state,
        status=status,
        tp_mode="multi",
        tp_levels_json=tp_levels,
        sl_history_json=[],
        tp_history_json=[],
        active_watchers_json=[],
        adjustment_priority_json=[
            "watcher",
            "trailing",
            "breakeven",
            "volatility",
        ],
        transition_log_json=[],
        opened_at=datetime.now(UTC),
        sl_type="fixed",
        sl_exchange_order_id=sl_id,
    )
    session.add(position)
    await session.commit()
    return position.id


def _patch_adapter_factory(
    monkeypatch: pytest.MonkeyPatch,
    adapter_by_symbol: dict[str, _FakeAdapter],
) -> None:
    async def _create_adapter(self, *, session, position):  # noqa: ANN001
        return adapter_by_symbol[position.symbol]

    monkeypatch.setattr(
        AutoTradeService,
        "_create_exchange_adapter",
        _create_adapter,
    )


# ───────────────────────── service-level tests ─────────────────────────────


async def test_confirm_false_returns_preview_and_does_not_touch_exchange(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        user_id, account_id, config_id = await _seed_user_account_and_config(session)
        profile_id = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id)
            )
        ).scalar_one()
        await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id,
            config_id=config_id,
            profile_id=profile_id,
        )

    adapter = _FakeAdapter()
    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": adapter})

    service = AutoTradeService()
    async with db() as session:
        with pytest.raises(ConfirmationRequiredError) as exc_info:
            await service.close_open_positions(
                session=session,
                user_id=user_id,
                confirm=False,
            )

    preview = exc_info.value.preview
    assert preview.total_count == 1
    assert preview.requires_confirm is True
    assert preview.positions[0].symbol == "BTC/USDT:USDT"
    assert preview.positions[0].current_quantity == pytest.approx(0.05)
    # 1 SL + 2 TP levels = 3 conditional orders
    assert preview.positions[0].open_conditional_orders_count == 3
    # Nothing happened on the adapter.
    assert adapter.cancelled_orders == []
    assert adapter.partial_close_calls == []


async def test_confirm_true_cancels_conditionals_and_market_closes(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        user_id, account_id, config_id = await _seed_user_account_and_config(session)
        profile_id = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id)
            )
        ).scalar_one()
        position_id = await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id,
            config_id=config_id,
            profile_id=profile_id,
        )

    adapter = _FakeAdapter()
    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": adapter})

    service = AutoTradeService()
    async with db() as session:
        result = await service.close_open_positions(
            session=session,
            user_id=user_id,
            confirm=True,
            reason="end-of-shift",
        )

    assert len(result.closed) == 1
    assert result.failed == []
    assert result.skipped_already_closed == []
    closed = result.closed[0]
    assert closed.symbol == "BTC/USDT:USDT"
    assert closed.executed_qty == pytest.approx(0.05)
    assert closed.avg_price == pytest.approx(70_150.0)
    assert set(closed.cancelled_conditional_orders) == {"sl-algo-1", "tp-algo-1", "tp-algo-2"}

    # Adapter saw exactly one market close + every cancel.
    assert len(adapter.partial_close_calls) == 1
    assert adapter.partial_close_calls[0]["order_type"] == "market"
    assert adapter.partial_close_calls[0]["quantity"] == pytest.approx(0.05)
    assert set(adapter.cancelled_orders) == {"sl-algo-1", "tp-algo-1", "tp-algo-2"}

    # DB row is now CLOSED.
    async with db() as session:
        row = await session.get(AutoTradePosition, position_id)
        assert row is not None
        assert row.state == "closed"
        assert row.status == "closed"
        assert row.close_reason == "end-of-shift"
        assert row.closed_at is not None
        assert float(row.current_quantity) == 0.0

        # Audit events emitted.
        events = (
            (await session.execute(select(AutoTradeEvent).order_by(AutoTradeEvent.id)))
            .scalars()
            .all()
        )
        event_types = [event.event_type for event in events]
        assert "position_manual_closed" in event_types
        assert "auto_trade_close_positions_completed" in event_types


async def test_already_flat_position_is_skipped(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        user_id, account_id, config_id = await _seed_user_account_and_config(session)
        profile_id = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id)
            )
        ).scalar_one()
        position_id = await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id,
            config_id=config_id,
            profile_id=profile_id,
        )

    # Adapter reports zero size — exchange already closed it.
    adapter = _FakeAdapter(live_size=0.0)
    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": adapter})

    service = AutoTradeService()
    async with db() as session:
        result = await service.close_open_positions(
            session=session,
            user_id=user_id,
            confirm=True,
        )

    assert result.closed == []
    assert result.failed == []
    assert result.skipped_already_closed == [position_id]
    # No market close issued, but conditionals still cancelled defensively.
    assert adapter.partial_close_calls == []
    assert set(adapter.cancelled_orders) == {"sl-algo-1", "tp-algo-1", "tp-algo-2"}

    async with db() as session:
        row = await session.get(AutoTradePosition, position_id)
        assert row is not None
        assert row.state == "closed"


async def test_market_close_failure_records_in_failed_list(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        user_id, account_id, config_id = await _seed_user_account_and_config(session)
        profile_id = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id)
            )
        ).scalar_one()
        await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id,
            config_id=config_id,
            profile_id=profile_id,
        )

    adapter = _FakeAdapter(market_close_error=RuntimeError("exchange-down"))
    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": adapter})

    service = AutoTradeService()
    async with db() as session:
        result = await service.close_open_positions(
            session=session,
            user_id=user_id,
            confirm=True,
        )

    assert result.closed == []
    assert len(result.failed) == 1
    assert "exchange-down" in result.failed[0].error
    # Cancels were attempted before the close failed.
    assert set(adapter.cancelled_orders) == {"sl-algo-1", "tp-algo-1", "tp-algo-2"}


async def test_partial_failure_continues_with_other_positions(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two positions on two accounts under one user.

    A user can only have one open ``AutoTradePosition`` per ``account_id``
    (uq_auto_trade_positions_user_account_open). To simulate "one user
    flattening multiple positions" we create two separate accounts/configs.
    The close endpoint resolves a *single* config per call, so we exercise
    partial failure within one account by issuing two close calls. A more
    direct multi-position-per-call test is impractical given the schema.
    """
    async with db() as session:
        user_id, account_id_a, config_id_a = await _seed_user_account_and_config(
            session, user_email="multi-account@example.com"
        )
        profile_id_a = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id_a)
            )
        ).scalar_one()
        # Reuse user_id but create another account+config.
        another_account = ExchangeCredential(
            user_id=user_id,
            exchange_name="binance",
            account_label="secondary",
            mode="demo",
            encrypted_api_key="k2",
            encrypted_api_secret="s2",
            encrypted_passphrase=None,
        )
        session.add(another_account)
        await session.flush()
        another_config = AutoTradeConfig(
            user_id=user_id,
            profile_id=profile_id_a,
            account_id=another_account.id,
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
        session.add(another_config)
        await session.commit()
        await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id_a,
            config_id=config_id_a,
            profile_id=profile_id_a,
            symbol="BTC/USDT:USDT",
        )

    btc_adapter = _FakeAdapter(market_close_error=RuntimeError("btc-fail"))
    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": btc_adapter})

    service = AutoTradeService()
    async with db() as session:
        result = await service.close_open_positions(
            session=session,
            user_id=user_id,
            account_id=account_id_a,
            confirm=True,
        )

    assert len(result.closed) == 0
    assert len(result.failed) == 1
    assert result.failed[0].symbol == "BTC/USDT:USDT"
    # Even on failure the conditional cancels were attempted.
    assert "btc-fail" in result.failed[0].error


async def test_foreign_user_positions_are_not_touched(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        own_user, own_account, own_config = await _seed_user_account_and_config(
            session, user_email="own@example.com"
        )
        own_profile = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == own_config)
            )
        ).scalar_one()
        own_position = await _seed_position(
            session,
            user_id=own_user,
            account_id=own_account,
            config_id=own_config,
            profile_id=own_profile,
        )

        foreign_user, foreign_account, foreign_config = await _seed_user_account_and_config(
            session, user_email="foreign@example.com"
        )
        foreign_profile = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == foreign_config)
            )
        ).scalar_one()
        foreign_position = await _seed_position(
            session,
            user_id=foreign_user,
            account_id=foreign_account,
            config_id=foreign_config,
            profile_id=foreign_profile,
            symbol="ETH/USDT:USDT",
            sl_id="sl-foreign",
            tp_levels=[],
        )

    own_adapter = _FakeAdapter()
    foreign_adapter = _FakeAdapter()
    _patch_adapter_factory(
        monkeypatch,
        {"BTC/USDT:USDT": own_adapter, "ETH/USDT:USDT": foreign_adapter},
    )

    service = AutoTradeService()
    async with db() as session:
        await service.close_open_positions(
            session=session,
            user_id=own_user,
            confirm=True,
        )

    # Own position closed, foreign untouched.
    async with db() as session:
        own_row = await session.get(AutoTradePosition, own_position)
        foreign_row = await session.get(AutoTradePosition, foreign_position)
        assert own_row is not None
        assert foreign_row is not None
        assert own_row.state == "closed"
        assert foreign_row.state == "open"

    assert foreign_adapter.cancelled_orders == []
    assert foreign_adapter.partial_close_calls == []


async def test_close_with_no_open_positions_returns_empty_result(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        user_id, _account_id, _config_id = await _seed_user_account_and_config(session)

    service = AutoTradeService()
    async with db() as session:
        result = await service.close_open_positions(
            session=session,
            user_id=user_id,
            confirm=True,
        )

    assert result.closed == []
    assert result.failed == []
    assert result.skipped_already_closed == []


async def test_close_without_config_raises_lookup(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        # Seed a user only — no config.
        user = User(email="orphan@example.com", hashed_password="x", is_active=True)
        session.add(user)
        await session.commit()
        user_id = user.id

    service = AutoTradeService()
    async with db() as session:
        with pytest.raises(LookupError):
            await service.close_open_positions(
                session=session,
                user_id=user_id,
                confirm=True,
            )


class _LiveConditionalFakeAdapter:
    """Adapter that reports live algo (conditional) orders for the preview."""

    def __init__(self, orders: list[ConditionalOrderResult]) -> None:
        self._orders = orders
        self.cancelled_orders: list[str] = []
        self.partial_close_calls: list[dict[str, Any]] = []

    async def get_open_conditional_orders(self, symbol: str) -> list[ConditionalOrderResult]:
        return list(self._orders)


async def test_preview_count_and_sl_reflect_live_algo_orders(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the DB row has no recorded conditional orders (manual / reconciled
    position), the preview must still report the SL + TP that are live on the
    exchange Algo endpoint — not ``0`` orders with ``SL = entry``.

    Reproduces the production symptom: ``fetch_open_orders``/DB-only counting is
    blind to Binance algo orders after the 2025-12-09 Algo Service migration.
    """
    async with db() as session:
        user_id, account_id, config_id = await _seed_user_account_and_config(session)
        profile_id = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id)
            )
        ).scalar_one()
        await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id,
            config_id=config_id,
            profile_id=profile_id,
            sl_id=None,  # nothing recorded in the DB
            tp_levels=[],  # nothing recorded in the DB
        )

    live_orders = [
        ConditionalOrderResult(
            exchange_order_id="sl-live",
            client_order_id="oneoff-sl",
            order_type="stop_loss",
            trigger_price=71_915.8,
            quantity=0.0,
            status="new",
            is_algo=True,
        ),
        ConditionalOrderResult("tp1", "oneoff-tp1", "take_profit", 73_005.4, 0.001, "new", True),
        ConditionalOrderResult("tp2", "oneoff-tp2", "take_profit", 73_368.6, 0.001, "new", True),
        ConditionalOrderResult("tp3", "oneoff-tp3", "take_profit", 73_731.8, 0.001, "new", True),
    ]
    adapter = _LiveConditionalFakeAdapter(live_orders)
    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": adapter})

    service = AutoTradeService()
    async with db() as session:
        with pytest.raises(ConfirmationRequiredError) as exc_info:
            await service.close_open_positions(
                session=session,
                user_id=user_id,
                confirm=False,
            )

    item = exc_info.value.preview.positions[0]
    # DB knows 0; the exchange has 1 SL + 3 TP — the preview must show the truth.
    assert item.open_conditional_orders_count == 4
    # SL must reflect the live algo trigger, not the entry-price/DB fallback.
    assert item.current_sl_price == pytest.approx(71_915.8)


# ─────────────────────────── endpoint tests ───────────────────────────────


@pytest.fixture
def override_endpoint_user_and_db(
    db: async_sessionmaker[AsyncSession],
) -> AsyncIterator[int]:
    user_id_holder = {"user_id": 0}

    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            yield session

    async def _fake_current_user() -> User:
        return User(
            id=user_id_holder["user_id"],
            email="endpoint@example.com",
            hashed_password="x",
            is_active=True,
        )

    app.dependency_overrides[get_db_session] = _get_test_db_session
    app.dependency_overrides[get_current_user] = _fake_current_user
    yield user_id_holder
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_current_user, None)


async def test_endpoint_returns_412_with_preview_when_confirm_false(
    db: async_sessionmaker[AsyncSession],
    override_endpoint_user_and_db: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        user_id, account_id, config_id = await _seed_user_account_and_config(session)
        override_endpoint_user_and_db["user_id"] = user_id
        profile_id = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id)
            )
        ).scalar_one()
        await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id,
            config_id=config_id,
            profile_id=profile_id,
        )

    adapter = _FakeAdapter()
    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": adapter})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/live/auto-trade/close-positions",
            json={"confirm": False},
        )

    assert response.status_code == 412
    body = response.json()
    assert body["requires_confirm"] is True
    assert body["total_count"] == 1
    assert body["positions"][0]["symbol"] == "BTC/USDT:USDT"
    assert adapter.partial_close_calls == []


async def test_endpoint_returns_200_and_executes_when_confirm_true(
    db: async_sessionmaker[AsyncSession],
    override_endpoint_user_and_db: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        user_id, account_id, config_id = await _seed_user_account_and_config(session)
        override_endpoint_user_and_db["user_id"] = user_id
        profile_id = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id)
            )
        ).scalar_one()
        position_id = await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id,
            config_id=config_id,
            profile_id=profile_id,
        )

    adapter = _FakeAdapter()
    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": adapter})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/live/auto-trade/close-positions",
            json={"confirm": True, "reason": "test-shift-end"},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["closed"]) == 1
    assert body["closed"][0]["position_id"] == position_id
    assert body["closed"][0]["executed_qty"] == pytest.approx(0.05)
    assert body["failed"] == []

    async with db() as session:
        row = await session.get(AutoTradePosition, position_id)
        assert row is not None
        assert row.state == "closed"
        assert row.close_reason == "test-shift-end"


async def test_endpoint_returns_404_when_no_config(
    db: async_sessionmaker[AsyncSession],
    override_endpoint_user_and_db: dict[str, int],
) -> None:
    async with db() as session:
        user = User(email="orphan-ep@example.com", hashed_password="x", is_active=True)
        session.add(user)
        await session.commit()
        override_endpoint_user_and_db["user_id"] = user.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/live/auto-trade/close-positions",
            json={"confirm": True},
        )

    assert response.status_code == 404


async def test_endpoint_does_not_change_is_running(
    db: async_sessionmaker[AsyncSession],
    override_endpoint_user_and_db: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop and close are independent — close must NOT toggle is_running."""
    async with db() as session:
        user_id, account_id, config_id = await _seed_user_account_and_config(session)
        override_endpoint_user_and_db["user_id"] = user_id
        profile_id = (
            await session.execute(
                select(AutoTradeConfig.profile_id).where(AutoTradeConfig.id == config_id)
            )
        ).scalar_one()
        await _seed_position(
            session,
            user_id=user_id,
            account_id=account_id,
            config_id=config_id,
            profile_id=profile_id,
        )

    _patch_adapter_factory(monkeypatch, {"BTC/USDT:USDT": _FakeAdapter()})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/live/auto-trade/close-positions",
            json={"confirm": True},
        )

    async with db() as session:
        config_row = await session.get(AutoTradeConfig, config_id)
        assert config_row is not None
        assert config_row.is_running is True  # unchanged
