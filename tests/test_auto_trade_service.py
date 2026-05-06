from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.auto_trade.service as auto_trade_service_module
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_event import AutoTradeEvent
from app.models.auto_trade_position import AutoTradePosition
from app.models.auto_trade_signal_queue import AutoTradeSignalQueue
from app.models.auto_trade_signal_state import AutoTradeSignalState
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_job import PersonalAnalysisJob
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.schemas.auto_trade import AutoTradeConfigUpsertRequest
from app.schemas.exchange_trading import (
    NormalizedFuturesPosition,
    NormalizedOrder,
    NormalizedTrade,
    OrderSide,
    SpotOrderRead,
)
from app.services.exchange.adapter import (
    ConditionalOrderResult,
    EntryOrderResult,
    OrderSide as ExchangeOrderSide,
)
from app.services.position.context import PositionContext
from app.services.position.state_machine import PositionState
from app.services.auto_trade.service import AutoTradeService
from app.services.execution.errors import ExchangeServiceError


class _FakeTradingService:
    def __init__(
        self,
        *,
        fail_close_order_attempts: int = 0,
        stale_position_reads_after_close: int = 0,
    ) -> None:
        self.leverage_calls: list[tuple[str, int]] = []
        self.order_calls: list[dict[str, object]] = []
        self.fetch_position_calls: list[str] = []
        self._counter = 0
        self._positions: dict[tuple[int | None, str], tuple[str, float]] = {}
        self._trades: dict[str, list[NormalizedTrade]] = {}
        self._fail_close_order_attempts = fail_close_order_attempts
        self._stale_position_reads_after_close = stale_position_reads_after_close
        self._stale_reads_remaining = 0

    def set_external_position(
        self,
        *,
        symbol: str,
        side: str,
        contracts: float,
        account_id: int | None = None,
    ) -> None:
        self._positions[(account_id, symbol)] = (side, float(contracts))

    def clear_external_position(self, *, symbol: str, account_id: int | None = None) -> None:
        if account_id is not None:
            self._positions.pop((account_id, symbol), None)
            return
        for key in list(self._positions):
            if key[1] == symbol:
                self._positions.pop(key, None)

    def set_symbol_trades(self, *, symbol: str, trades: list[NormalizedTrade]) -> None:
        self._trades[symbol] = trades

    async def set_futures_leverage(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        leverage: int,
    ) -> None:
        self.leverage_calls.append((symbol, leverage))

    async def place_futures_market_order(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        side: str,
        amount: float,
        reduce_only: bool,
        client_order_id: str | None = None,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
    ) -> SpotOrderRead:
        self._counter += 1
        if reduce_only and self._fail_close_order_attempts > 0:
            self._fail_close_order_attempts -= 1
            raise ExchangeServiceError(
                code="temporary_unavailable",
                message="Temporary close error.",
                retryable=True,
            )

        self.order_calls.append(
            {
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "reduce_only": reduce_only,
                "client_order_id": client_order_id,
                "take_profit_price": take_profit_price,
                "stop_loss_price": stop_loss_price,
            }
        )
        if not reduce_only:
            position_side = "long" if side == "buy" else "short"
            self._positions[(account_id, symbol)] = (position_side, float(amount))
        else:
            self._positions.pop((account_id, symbol), None)
            self._positions.pop((None, symbol), None)
            self._stale_reads_remaining = self._stale_position_reads_after_close

        fill_price = 100.0 if not reduce_only else 95.0
        order = NormalizedOrder(
            id=f"ord-{self._counter}",
            client_order_id=client_order_id,
            symbol=symbol,
            side=cast(OrderSide, side),
            order_type="market",
            status="closed",
            amount=float(amount),
            filled=float(amount),
            remaining=0.0,
            price=fill_price,
            average=fill_price,
            cost=fill_price * float(amount),
            timestamp=datetime.now(UTC),
            raw={"reduceOnly": reduce_only},
        )
        return SpotOrderRead(
            account_id=account_id,
            exchange_name="bybit",
            mode="demo",
            order=order,
        )

    async def close_futures_market_reduce_only(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        side: str,
        amount: float,
        client_order_id: str | None = None,
    ) -> SpotOrderRead:
        return await self.place_futures_market_order(
            session=session,
            user_id=user_id,
            account_id=account_id,
            symbol=symbol,
            side=side,
            amount=amount,
            reduce_only=True,
            client_order_id=client_order_id,
            take_profit_price=None,
            stop_loss_price=None,
        )

    async def fetch_futures_position(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
    ) -> NormalizedFuturesPosition | None:
        self.fetch_position_calls.append(symbol)
        position = self._positions.get((account_id, symbol))
        if position is None:
            position = self._positions.get((None, symbol))
        if position is None:
            if self._stale_reads_remaining > 0:
                self._stale_reads_remaining -= 1
                return NormalizedFuturesPosition(
                    symbol=symbol,
                    side="long",
                    contracts=1.0,
                    entry_price=100.0,
                    mark_price=100.0,
                    leverage=1.0,
                    unrealized_pnl=0.0,
                    raw={"stale": True},
                )
            return None

        side_name, contracts = position
        return NormalizedFuturesPosition(
            symbol=symbol,
            side=cast(Any, side_name),
            contracts=contracts,
            entry_price=100.0,
            mark_price=101.0,
            leverage=1.0,
            unrealized_pnl=0.0,
            raw={},
        )

    async def fetch_futures_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[NormalizedTrade]:
        rows = list(self._trades.get(symbol, []))
        if since is not None:
            filtered: list[NormalizedTrade] = []
            for row in rows:
                if row.timestamp is None:
                    filtered.append(row)
                    continue
                try:
                    if row.timestamp >= since:
                        filtered.append(row)
                except TypeError:
                    filtered.append(row)
            rows = filtered
        return rows[:limit]


class _ImmediateQueue:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    async def enqueue(self, task: object) -> None:
        self.tasks.append(task)
        callback = getattr(task, "on_success", None)
        if callback is None:
            return

        params = getattr(task, "params", {})
        trigger_price = float(params.get("trigger_price", params.get("new_trigger_price", 0.0)))
        quantity = float(params.get("quantity", params.get("new_quantity", 0.0)))
        order_type = "stop_loss" if getattr(task, "action", "") == "place_sl" else "take_profit"
        await callback(
            ConditionalOrderResult(
                exchange_order_id=f"{getattr(task, 'action', 'task')}-{len(self.tasks)}",
                client_order_id=str(params.get("client_order_id", "")),
                order_type=order_type,
                trigger_price=trigger_price,
                quantity=quantity,
                status="new",
                is_algo=False,
            )
        )


def test_auto_trade_risk_mode_accepts_decimal_ratio() -> None:
    payload = AutoTradeConfigUpsertRequest(
        enabled=True,
        profile_id=1,
        account_id=1,
        position_size_usdt=100.0,
        leverage=1,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2,5",
        sl_pct=1.0,
        tp_pct=2.5,
    )
    assert payload.risk_mode == "1:2.5"


@pytest.fixture
async def auto_trade_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "auto_trade_service.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _seed_user_profile_and_account(
    session: AsyncSession,
) -> tuple[User, PersonalAnalysisProfile, int]:
    user = User(email="auto@example.com", hashed_password="x", is_active=True)
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
    await session.commit()
    await session.refresh(user)
    await session.refresh(profile)
    await session.refresh(account)
    return user, profile, account.id


async def _create_profile_and_account(
    session: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    account_label: str,
) -> tuple[PersonalAnalysisProfile, int]:
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
    account = ExchangeCredential(
        user_id=user_id,
        exchange_name="bybit",
        account_label=account_label,
        mode="demo",
        encrypted_api_key=f"k-{account_label}",
        encrypted_api_secret=f"s-{account_label}",
        encrypted_passphrase=None,
    )
    session.add(account)
    await session.commit()
    await session.refresh(profile)
    await session.refresh(account)
    return profile, account.id


def _build_signal(
    *,
    trend: str,
    confidence_pct: float,
    symbol: str = "BTCUSDT",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "symbol": symbol,
        "trend": trend,
        "confidence_pct": confidence_pct,
        "price": {"current": 100.0},
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _strategy_profile_payload() -> dict[str, object]:
    return {
        "sl_mode": "atr",
        "sl_value": 2.0,
        "tp_mode": "multi",
        "tp_levels": [
            {"price_offset_pct": 1.5, "close_pct": 33.3, "move_sl_to": "breakeven"},
            {"price_offset_pct": 3.0, "close_pct": 33.3, "move_sl_to": "tp1"},
            {"price_offset_pct": 5.0, "close_pct": 33.4, "move_sl_to": None},
        ],
        "trailing_enabled": True,
        "trailing_callback_rate": 1.5,
        "breakeven_enabled": True,
        "breakeven_trigger_rr": 1.2,
        "volatility_sl_enabled": True,
        "volatility_atr_period": 14,
        "volatility_atr_multiplier": 2.5,
        "watchers": [
            {
                "indicator": "rsi",
                "params": {"period": 14, "timeframe": "15m"},
                "condition": "> 75",
                "action": "tighten_sl",
                "action_params": {"sl_offset_atr": 1.5},
                "is_active": True,
            }
        ],
        "adjustment_priority": ["watcher", "trailing", "breakeven", "volatility"],
        "max_position_pct": 50.0,
        "allow_sl_widen": True,
    }


def _build_legacy_signal(
    *,
    bias: str,
    confidence: float,
    symbol: str = "BTCUSDT",
) -> dict[str, object]:
    return {
        "analysisStructured": {
            "symbol": symbol,
            "bias": bias,
            "confidence": confidence,
            "currentPrice": 100.0,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    }


async def _create_history(
    session: AsyncSession,
    *,
    user_id: int,
    profile_id: int,
    trade_job_id: str,
    signal_payload: dict[str, object],
) -> PersonalAnalysisHistory:
    now = datetime.now(UTC)
    job = PersonalAnalysisJob(
        id=trade_job_id,
        user_id=user_id,
        profile_id=profile_id,
        core_job_id=f"core-{trade_job_id}",
        status="completed",
        attempt=1,
        max_attempts=3,
        error=None,
        payload_json={"symbol": "BTCUSDT"},
        next_poll_at=now,
        completed_at=now,
        core_deleted_at=now,
    )
    session.add(job)
    await session.flush()
    history = PersonalAnalysisHistory(
        user_id=user_id,
        profile_id=profile_id,
        trade_job_id=job.id,
        symbol="BTCUSDT",
        analysis_data=signal_payload,
        core_completed_at=now,
    )
    session.add(history)
    await session.commit()
    await session.refresh(history)
    return history


async def _create_and_run_config(
    session: AsyncSession,
    *,
    service: AutoTradeService,
    user_id: int,
    profile_id: int,
    account_id: int,
) -> None:
    payload = AutoTradeConfigUpsertRequest(
        enabled=True,
        profile_id=profile_id,
        account_id=account_id,
        position_size_usdt=100.0,
        leverage=1,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
    )
    await service.upsert_config(session=session, user_id=user_id, payload=payload)
    await service.set_running(session=session, user_id=user_id, is_running=True)


async def test_auto_trade_config_accepts_binance_account(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        account = await session.get(ExchangeCredential, account_id)
        assert account is not None
        account.exchange_name = "binance"
        await session.commit()

        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=3,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        assert config.account_id == account_id


async def test_auto_trade_upsert_config_persists_strategy_profile_json(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)

        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=3,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
                strategy_profile=_strategy_profile_payload(),
            ),
        )

        assert config.strategy_profile_json is not None
        assert config.strategy_profile_json["sl_mode"] == "atr"
        assert config.strategy_profile_json["watchers"] == [
            {
                "indicator": "RSI",
                "params": {"period": 14, "timeframe": "15m"},
                "condition": "> 75",
                "action": "tighten_sl",
                "action_params": {"sl_offset_atr": 1.5},
                "is_active": True,
            }
        ]
        assert config.strategy_profile == config.strategy_profile_json


async def test_auto_trade_upsert_config_preserves_strategy_profile_when_omitted(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        created = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=3,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
                strategy_profile=_strategy_profile_payload(),
            ),
        )
        original_strategy_profile = created.strategy_profile_json

        updated = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=False,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=150.0,
                leverage=5,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )

        assert updated.enabled is False
        assert updated.position_size_usdt == pytest.approx(150.0)
        assert updated.strategy_profile_json == original_strategy_profile


async def test_auto_trade_exchange_adapter_path_initializes_runtime_context_and_queue(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(
        trading_service=cast(Any, fake_trading),
        use_exchange_adapter_entry=True,
    )
    queue = _ImmediateQueue()
    fake_adapter = AsyncMock()
    fake_adapter.place_entry_order = AsyncMock(
        return_value=EntryOrderResult(
            exchange_order_id="entry-1",
            client_order_id="adapter-entry-1",
            symbol="BTC/USDT:USDT",
            side=ExchangeOrderSide.BUY,
            order_type="market",
            status="closed",
            quantity=1.0,
            filled_quantity=1.0,
            remaining_quantity=0.0,
            price=100.0,
            average_price=100.0,
            cost=100.0,
            timestamp=datetime.now(UTC),
            raw={"source": "adapter"},
        )
    )
    schedule_watchers = AsyncMock(return_value="watcher-schedule-1")
    ensure_ws_manager = AsyncMock()

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
                strategy_profile=_strategy_profile_payload(),
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)

        monkeypatch.setattr(service, "_create_exchange_adapter", AsyncMock(return_value=fake_adapter))
        monkeypatch.setattr("app.services.auto_trade.service.get_order_queue", AsyncMock(return_value=queue))
        monkeypatch.setattr(service, "_schedule_position_watchers", schedule_watchers)
        monkeypatch.setattr(service, "_ensure_ws_manager_tracked", ensure_ws_manager)

        async def _persist_runtime(position_context: PositionContext) -> None:
            row = await session.get(AutoTradePosition, int(position_context.position_id))
            assert row is not None
            service._merge_position_context_into_row(row=row, position=position_context)
            await session.flush()

        monkeypatch.setattr(service, "_persist_runtime_position", _persist_runtime)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-adapter-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        fake_adapter.place_entry_order.assert_awaited_once()
        assert fake_trading.order_calls == []
        assert fake_trading.leverage_calls == [("BTC/USDT:USDT", 1)]
        schedule_watchers.assert_awaited_once()
        ensure_ws_manager.assert_awaited_once()

        position = cast(
            AutoTradePosition | None,
            await session.scalar(
                select(AutoTradePosition)
                .where(AutoTradePosition.user_id == user.id)
                .order_by(AutoTradePosition.id.desc())
                .limit(1)
            ),
        )
        assert position is not None
        assert position.status == "open"
        assert position.state == "open"
        assert position.sl_type == "atr"
        assert position.tp_mode == "multi"
        assert float(position.original_quantity or 0) == pytest.approx(1.0)
        assert float(position.current_quantity or 0) == pytest.approx(1.0)
        assert position.sl_exchange_order_id == "place_sl-1"
        assert position.active_watchers_json[0]["indicator"] == "RSI"
        assert [task.action for task in queue.tasks] == [
            "place_sl",
            "place_tp",
            "place_tp",
            "place_tp",
        ]
        assert queue.tasks[0].params["trigger_price"] == pytest.approx(99.0)
        assert len(position.tp_levels_json) == 3
        assert [level["exchange_order_id"] for level in position.tp_levels_json] == [
            "place_tp-2",
            "place_tp-3",
            "place_tp-4",
        ]
        assert all(level["status"] == "open" for level in position.tp_levels_json)
        assert [entry["trigger"] for entry in position.transition_log_json] == [
            "entry_submitted",
            "entry_filled",
        ]


async def test_ensure_ws_manager_reuses_single_account_manager(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(
        trading_service=cast(Any, fake_trading),
        use_exchange_adapter_entry=True,
    )
    fake_adapter = AsyncMock()
    monkeypatch.setattr(service, "_create_exchange_adapter", AsyncMock(return_value=fake_adapter))

    created_managers: list[object] = []

    class _FakeManager:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.started = 0
            self.tracked: list[str] = []
            created_managers.append(self)

        async def start(self) -> None:
            self.started += 1

        def track_position(self, position: PositionContext) -> None:
            self.tracked.append(position.position_id)

    monkeypatch.setattr("app.services.auto_trade.service.WebSocketManager", _FakeManager)
    auto_trade_service_module._WS_MANAGER_REGISTRY.clear()

    async with auto_trade_db() as session:
        first = PositionContext(
            position_id="1",
            user_id="10",
            account_id="42",
            exchange="binance",
            symbol="BTC/USDT:USDT",
            state=PositionState.OPEN,
        )
        second = PositionContext(
            position_id="2",
            user_id="10",
            account_id="42",
            exchange="binance",
            symbol="ETH/USDT:USDT",
            state=PositionState.OPEN,
        )

        await service._ensure_ws_manager_tracked(session=session, position=first)
        await service._ensure_ws_manager_tracked(session=session, position=second)

    assert len(created_managers) == 1
    manager = cast(Any, created_managers[0])
    assert manager.started == 1
    assert manager.tracked == ["1", "2"]
    auto_trade_service_module._WS_MANAGER_REGISTRY.clear()


async def test_auto_trade_opens_and_closes_after_two_opposite_reports(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )

        first = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        assert await service.enqueue_history_signal(session=session, history=first) is True
        first_stats = await service.process_signal_queue(session=session)
        assert first_stats["completed"] == 1

        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is not None
        assert open_position.side == "LONG"
        assert open_position.status == "open"
        assert open_position.symbol == "BTC/USDT:USDT"
        assert fake_trading.leverage_calls[0][0] == "BTC/USDT:USDT"

        second = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-opposite-1",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=70.0),
        )
        assert await service.enqueue_history_signal(session=session, history=second) is True
        second_stats = await service.process_signal_queue(session=session)
        assert second_stats["completed"] == 1
        still_open = await service.get_open_position(session=session, user_id=user.id)
        assert still_open is not None
        assert still_open.status == "open"

        third = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-opposite-2",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=70.0),
        )
        assert await service.enqueue_history_signal(session=session, history=third) is True
        third_stats = await service.process_signal_queue(session=session)
        assert third_stats["completed"] == 1

        open_after_close = await service.get_open_position(session=session, user_id=user.id)
        assert open_after_close is None

        closed_positions = list(
            (
                await session.scalars(
                    select(AutoTradePosition).where(
                        AutoTradePosition.user_id == user.id,
                        AutoTradePosition.status == "closed",
                    )
                )
            ).all()
        )
        assert len(closed_positions) == 1
        assert closed_positions[0].close_reason == "opposite_confirmed"
        assert len(fake_trading.order_calls) == 2
        assert fake_trading.order_calls[-1]["reduce_only"] is True


async def test_auto_trade_fast_closes_on_high_confidence_opposite(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )

        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-fast",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        fast_close_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-fast-close",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=85.0),
        )
        await service.enqueue_history_signal(session=session, history=fast_close_history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is None
        closed = cast(
            AutoTradePosition | None,
            await session.scalar(
                select(AutoTradePosition)
                .where(AutoTradePosition.user_id == user.id)
                .order_by(AutoTradePosition.id.desc())
                .limit(1)
            ),
        )
        assert closed is not None
        assert closed.status == "closed"
        assert closed.close_reason == "opposite_fast_confidence"
        assert len(fake_trading.fetch_position_calls) >= 2


async def test_auto_trade_syncs_manual_exchange_close_into_db(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-external-close",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        fake_trading.clear_external_position(symbol="BTC/USDT:USDT")
        synced_open_position = await service.get_open_position(session=session, user_id=user.id)
        assert synced_open_position is None

        closed_position = cast(
            AutoTradePosition | None,
            await session.scalar(
                select(AutoTradePosition)
                .where(AutoTradePosition.user_id == user.id, AutoTradePosition.status == "closed")
                .order_by(AutoTradePosition.id.desc())
                .limit(1)
            ),
        )
        assert closed_position is not None
        assert closed_position.close_reason == "already_closed_on_exchange"
        assert closed_position.close_price is None


async def test_auto_trade_positions_list_syncs_exchange_before_read(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-list-sync",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        fake_trading.clear_external_position(symbol="BTC/USDT:USDT")
        open_rows = await service.list_positions(
            session=session,
            user_id=user.id,
            limit=20,
            status="open",
        )
        assert open_rows == []
        closed_rows = await service.list_positions(
            session=session,
            user_id=user.id,
            limit=20,
            status="closed",
        )
        assert len(closed_rows) == 1
        assert closed_rows[0].close_reason == "already_closed_on_exchange"


async def test_auto_trade_uses_exchange_open_position_when_db_is_empty(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        fake_trading.set_external_position(
            symbol="BTC/USDT:USDT",
            side="short",
            contracts=1.0,
        )
        opposite_signal = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-close-external-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=85.0),
        )
        await service.enqueue_history_signal(session=session, history=opposite_signal)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1
        assert len(fake_trading.order_calls) == 1
        assert fake_trading.order_calls[0]["reduce_only"] is True
        assert await service.get_open_position(session=session, user_id=user.id) is None
        closed_position = cast(
            AutoTradePosition | None,
            await session.scalar(
                select(AutoTradePosition)
                .where(AutoTradePosition.user_id == user.id, AutoTradePosition.status == "closed")
                .order_by(AutoTradePosition.id.desc())
                .limit(1)
            ),
        )
        assert closed_position is not None
        assert closed_position.side == "SHORT"
        assert closed_position.close_reason == "opposite_fast_confidence"


async def test_auto_trade_invalid_payload_is_skipped_without_orders(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        invalid_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-invalid",
            signal_payload={"trend": "LONG"},
        )
        assert (
            await service.enqueue_history_signal(session=session, history=invalid_history) is True
        )
        stats = await service.process_signal_queue(session=session)
        assert stats["skipped"] == 1

        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is None
        assert fake_trading.order_calls == []

        events = list(
            (
                await session.scalars(
                    select(AutoTradeEvent).where(
                        AutoTradeEvent.user_id == user.id,
                        AutoTradeEvent.event_type == "signal_skipped_invalid_payload",
                    )
                )
            ).all()
        )
        assert len(events) == 1


async def test_auto_trade_legacy_payload_is_adapted_and_opens_position(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )

        legacy_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-legacy",
            signal_payload=_build_legacy_signal(bias="BULLISH", confidence=0.7),
        )
        assert await service.enqueue_history_signal(session=session, history=legacy_history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is not None
        assert open_position.side == "LONG"
        assert open_position.status == "open"
        assert len(fake_trading.order_calls) == 1


async def test_auto_trade_enqueue_is_idempotent_for_same_history(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-idempotent",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        first = await service.enqueue_history_signal(session=session, history=history)
        second = await service.enqueue_history_signal(session=session, history=history)
        assert first is True
        assert second is False

        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1
        total_positions = int((await session.scalar(select(func.count(AutoTradePosition.id)))) or 0)
        assert total_positions == 1
        signal_state = cast(
            AutoTradeSignalState | None,
            await session.scalar(
                select(AutoTradeSignalState).where(AutoTradeSignalState.user_id == user.id)
            ),
        )
        assert signal_state is not None
        assert signal_state.last_processed_history_id == history.id


async def test_auto_trade_does_not_open_when_confidence_below_min(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        low_conf = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-low-confidence",
            signal_payload=_build_signal(trend="LONG", confidence_pct=61.9),
        )
        await service.enqueue_history_signal(session=session, history=low_conf)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1
        assert await service.get_open_position(session=session, user_id=user.id) is None
        assert fake_trading.order_calls == []


async def test_auto_trade_neutral_does_not_open_or_close(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )

        neutral = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-neutral-open",
            signal_payload=_build_signal(trend="NEUTRAL", confidence_pct=90.0),
        )
        await service.enqueue_history_signal(session=session, history=neutral)
        await service.process_signal_queue(session=session)
        assert await service.get_open_position(session=session, user_id=user.id) is None

        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-neutral-open-2",
            signal_payload=_build_signal(trend="LONG", confidence_pct=75.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)
        assert await service.get_open_position(session=session, user_id=user.id) is not None

        neutral_hold = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-neutral-hold",
            signal_payload=_build_signal(trend="NEUTRAL", confidence_pct=90.0),
        )
        await service.enqueue_history_signal(session=session, history=neutral_hold)
        await service.process_signal_queue(session=session)
        assert await service.get_open_position(session=session, user_id=user.id) is not None
        assert len(fake_trading.order_calls) == 1


async def test_auto_trade_retries_close_until_exchange_confirms(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService(stale_position_reads_after_close=3)
    service = AutoTradeService(trading_service=cast(Any, fake_trading))
    service._retry_interval_seconds = 1

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-for-confirm",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        close_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-close-confirm",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=85.0),
        )
        await service.enqueue_history_signal(session=session, history=close_history)
        first_stats = await service.process_signal_queue(session=session)
        assert first_stats["retried"] == 1

        queue_item = cast(
            AutoTradeSignalQueue | None,
            await session.scalar(
                select(AutoTradeSignalQueue).where(
                    AutoTradeSignalQueue.history_id == close_history.id
                )
            ),
        )
        assert queue_item is not None
        queue_item.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        second_stats = await service.process_signal_queue(session=session)
        assert second_stats["completed"] == 1
        assert await service.get_open_position(session=session, user_id=user.id) is None


async def test_auto_trade_queue_moves_to_dead_after_retry_limit(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService(fail_close_order_attempts=10)
    service = AutoTradeService(trading_service=cast(Any, fake_trading))
    service._retry_interval_seconds = 1

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-for-dead",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        close_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-close-dead",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=85.0),
        )
        await service.enqueue_history_signal(session=session, history=close_history)

        for _ in range(service._max_attempts):
            await service.process_signal_queue(session=session)
            queue_item = cast(
                AutoTradeSignalQueue | None,
                await session.scalar(
                    select(AutoTradeSignalQueue).where(
                        AutoTradeSignalQueue.history_id == close_history.id
                    )
                ),
            )
            assert queue_item is not None
            if queue_item.status == "dead":
                break
            queue_item.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        queue_item = cast(
            AutoTradeSignalQueue | None,
            await session.scalar(
                select(AutoTradeSignalQueue).where(
                    AutoTradeSignalQueue.history_id == close_history.id
                )
            ),
        )
        assert queue_item is not None
        assert queue_item.status == "dead"


async def test_auto_trade_builds_open_position_pnl_snapshot(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-pnl-snapshot",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is not None
        snapshot = await service.build_position_pnl_snapshot(
            session=session,
            user_id=user.id,
            position=open_position,
        )
        assert snapshot["position_id"] == open_position.id
        assert snapshot["source"] == "exchange"
        assert snapshot["symbol"] == "BTC/USDT:USDT"
        assert snapshot["chart_symbol"] == "BTC/USDT"
        assert snapshot["status"] == "open"
        assert snapshot["entry_notional_usdt"] == 100.0
        assert snapshot["realized_pnl_usdt"] == 0.0
        assert snapshot["total_pnl_usdt"] == snapshot["unrealized_pnl_usdt"]


async def test_auto_trade_builds_closed_position_pnl_snapshot(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-pnl-close",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        close_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-close-pnl-close",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=90.0),
        )
        await service.enqueue_history_signal(session=session, history=close_history)
        await service.process_signal_queue(session=session)

        closed_position = cast(
            AutoTradePosition | None,
            await session.scalar(
                select(AutoTradePosition)
                .where(AutoTradePosition.user_id == user.id, AutoTradePosition.status == "closed")
                .order_by(AutoTradePosition.id.desc())
                .limit(1)
            ),
        )
        assert closed_position is not None
        snapshot = await service.build_position_pnl_snapshot(
            session=session,
            user_id=user.id,
            position=closed_position,
        )
        assert snapshot["source"] == "closed"
        assert snapshot["close_price"] == 95.0
        assert snapshot["realized_pnl_usdt"] == pytest.approx(-5.0)
        assert snapshot["unrealized_pnl_usdt"] == 0.0
        assert snapshot["total_pnl_usdt"] == pytest.approx(-5.0)


async def test_auto_trade_closed_snapshot_derives_exit_from_trades_when_missing_close_price(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        now = datetime.now(UTC)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        position = AutoTradePosition(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            side="LONG",
            status="closed",
            entry_price=100.0,
            quantity=1.0,
            position_size_usdt=100.0,
            leverage=1,
            tp_price=110.0,
            sl_price=90.0,
            entry_confidence_pct=0.0,
            opened_at=now - timedelta(minutes=5),
            closed_at=now - timedelta(minutes=1),
            close_reason="already_closed_on_exchange",
            close_price=None,
            open_order_id=None,
            close_order_id=None,
            open_history_id=None,
            close_history_id=None,
            raw_open_order={},
            raw_close_order={},
        )
        session.add(position)
        await session.commit()
        await session.refresh(position)

        fake_trading.set_symbol_trades(
            symbol="BTC/USDT:USDT",
            trades=[
                NormalizedTrade(
                    id="t-close-1",
                    order_id=None,
                    symbol="BTC/USDT:USDT",
                    side="sell",
                    amount=1.0,
                    price=105.0,
                    cost=105.0,
                    fee_cost=0.0,
                    fee_currency="USDT",
                    timestamp=now - timedelta(minutes=1),
                    raw={"info": {"closedPnl": "5.0"}},
                )
            ],
        )
        inferred = await service._infer_closed_position_from_trades(
            session=session,
            user_id=user.id,
            position=position,
        )
        assert inferred is not None
        snapshot = await service.build_position_pnl_snapshot(
            session=session,
            user_id=user.id,
            position=position,
        )
        assert snapshot["source"] == "derived"
        assert snapshot["close_price"] == pytest.approx(105.0)
        assert snapshot["total_pnl_usdt"] == pytest.approx(5.0)


async def test_auto_trade_positions_summary_aggregates_pnl(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-summary",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        close_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-close-summary",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=90.0),
        )
        await service.enqueue_history_signal(session=session, history=close_history)
        await service.process_signal_queue(session=session)

        summary_payload = await service.summarize_positions_pnl(
            session=session,
            user_id=user.id,
            limit=20,
            status=None,
        )
        assert summary_payload["summary"]["total_positions"] == 1
        assert summary_payload["summary"]["open_positions"] == 0
        assert summary_payload["summary"]["closed_positions"] == 1
        assert summary_payload["summary"]["total_realized_pnl_usdt"] == pytest.approx(-5.0)
        assert summary_payload["summary"]["total_unrealized_pnl_usdt"] == 0.0
        assert summary_payload["summary"]["total_pnl_usdt"] == pytest.approx(-5.0)
        assert summary_payload["summary"]["total_trade_pnl_usdt"] == pytest.approx(-5.0)
        assert len(summary_payload["positions"]) == 1
        assert "lifecycle" in summary_payload["positions"][0]
        assert summary_payload["positions"][0]["trade_pnl_usdt"] == pytest.approx(-5.0)


async def test_auto_trade_cannot_change_profile_or_account_when_running_or_open(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        profile2, account2_id = await _create_profile_and_account(
            session,
            user_id=user.id,
            symbol="ETHUSDT",
            account_label="secondary",
        )
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )

        with pytest.raises(ValueError):
            await service.upsert_config(
                session=session,
                user_id=user.id,
                payload=AutoTradeConfigUpsertRequest(
                    enabled=True,
                    profile_id=profile2.id,
                    account_id=account_id,
                    position_size_usdt=100.0,
                    leverage=1,
                    min_confidence_pct=62.0,
                    fast_close_confidence_pct=80.0,
                    confirm_reports_required=2,
                    risk_mode="1:2",
                    sl_pct=1.0,
                    tp_pct=2.0,
                ),
            )

        open_history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-open-for-change-block",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)
        await service.set_running(session=session, user_id=user.id, is_running=False)

        with pytest.raises(ValueError):
            await service.upsert_config(
                session=session,
                user_id=user.id,
                payload=AutoTradeConfigUpsertRequest(
                    enabled=False,
                    profile_id=profile2.id,
                    account_id=account_id,
                    position_size_usdt=100.0,
                    leverage=1,
                    min_confidence_pct=62.0,
                    fast_close_confidence_pct=80.0,
                    confirm_reports_required=2,
                    risk_mode="1:2",
                    sl_pct=1.0,
                    tp_pct=2.0,
                ),
            )


async def test_auto_trade_running_account_does_not_block_other_account_config(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        profile2, account2_id = await _create_profile_and_account(
            session,
            user_id=user.id,
            symbol="ETHUSDT",
            account_label="secondary",
        )
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )

        config2 = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile2.id,
                account_id=account2_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        assert config2.account_id == account2_id
        assert config2.is_running is False


async def test_auto_trade_enqueues_same_history_for_each_active_account_config(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        _, account2_id = await _create_profile_and_account(
            session,
            user_id=user.id,
            symbol=profile.symbol,
            account_label="secondary-fanout",
        )
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account2_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        await service.set_running(
            session=session,
            user_id=user.id,
            is_running=True,
            account_id=account2_id,
        )

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-fanout",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        first = await service.enqueue_history_signal(session=session, history=history)
        second = await service.enqueue_history_signal(session=session, history=history)
        assert first is True
        assert second is False

        queue_rows = list(
            (
                await session.scalars(
                    select(AutoTradeSignalQueue).where(
                        AutoTradeSignalQueue.history_id == history.id
                    )
                )
            ).all()
        )
        assert len(queue_rows) == 2
        assert len({row.config_id for row in queue_rows}) == 2


async def test_auto_trade_client_order_id_includes_config_scope(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        _, account2_id = await _create_profile_and_account(
            session,
            user_id=user.id,
            symbol=profile.symbol,
            account_label="secondary-order-id",
        )
        await _create_and_run_config(
            session,
            service=service,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
        )
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account2_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        await service.set_running(
            session=session,
            user_id=user.id,
            is_running=True,
            account_id=account2_id,
        )

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-order-id-scope",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 2

        configs = list(
            (
                await session.scalars(
                    select(AutoTradeConfig).where(
                        AutoTradeConfig.user_id == user.id,
                        AutoTradeConfig.profile_id == profile.id,
                    )
                )
            ).all()
        )
        config_ids = {config.id for config in configs}

        open_client_order_ids = {
            str(call.get("client_order_id"))
            for call in fake_trading.order_calls
            if call.get("reduce_only") is False
        }
        assert len(open_client_order_ids) == 2
        for config_id in config_ids:
            assert any(f"-{config_id}-" in order_id for order_id in open_client_order_ids)


# ─── Bracket TP/SL on entry ───────────────────────────────────────────────────


def _build_entry_result(
    *,
    side: ExchangeOrderSide,
    quantity: float = 1.0,
    attached_sl: ConditionalOrderResult | None = None,
    attached_tp: ConditionalOrderResult | None = None,
) -> EntryOrderResult:
    return EntryOrderResult(
        exchange_order_id="entry-1",
        client_order_id="adapter-entry-1",
        symbol="BTC/USDT:USDT",
        side=side,
        order_type="market",
        status="closed",
        quantity=quantity,
        filled_quantity=quantity,
        remaining_quantity=0.0,
        price=100.0,
        average_price=100.0,
        cost=100.0 * quantity,
        timestamp=datetime.now(UTC),
        raw={"source": "adapter"},
        attached_sl=attached_sl,
        attached_tp=attached_tp,
    )


def _bracket_conditional_result(
    *,
    order_type: str,
    trigger_price: float,
    quantity: float = 1.0,
) -> ConditionalOrderResult:
    return ConditionalOrderResult(
        exchange_order_id=f"bracket-{order_type}-1",
        client_order_id=f"adapter-entry-1-{order_type[:2]}",
        order_type=order_type,
        trigger_price=trigger_price,
        quantity=quantity,
        status="new",
        is_algo=False,
    )


async def test_signal_open_with_bracket_attaches_protective_orders_legacy(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Legacy CCXT path must forward computed tp_price/sl_price to the trading service."""
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-bracket-legacy",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        entry_calls = [c for c in fake_trading.order_calls if c.get("reduce_only") is False]
        assert len(entry_calls) == 1
        opened = entry_calls[0]
        assert opened["take_profit_price"] == pytest.approx(102.0)
        assert opened["stop_loss_price"] == pytest.approx(99.0)


async def test_signal_open_with_bracket_skips_queue_enqueue_for_single_tp(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter path: when bracket attaches both SL and TP, queue must not duplicate them."""
    fake_trading = _FakeTradingService()
    service = AutoTradeService(
        trading_service=cast(Any, fake_trading),
        use_exchange_adapter_entry=True,
    )
    queue = _ImmediateQueue()
    fake_adapter = AsyncMock()
    fake_adapter.place_entry_order = AsyncMock(
        return_value=_build_entry_result(
            side=ExchangeOrderSide.BUY,
            attached_sl=_bracket_conditional_result(
                order_type="stop_loss", trigger_price=99.0
            ),
            attached_tp=_bracket_conditional_result(
                order_type="take_profit", trigger_price=102.0
            ),
        )
    )

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)

        monkeypatch.setattr(
            service, "_create_exchange_adapter", AsyncMock(return_value=fake_adapter)
        )
        monkeypatch.setattr(
            "app.services.auto_trade.service.get_order_queue",
            AsyncMock(return_value=queue),
        )
        monkeypatch.setattr(service, "_schedule_position_watchers", AsyncMock())
        monkeypatch.setattr(service, "_ensure_ws_manager_tracked", AsyncMock())

        async def _persist_runtime(position_context: PositionContext) -> None:
            row = await session.get(AutoTradePosition, int(position_context.position_id))
            assert row is not None
            service._merge_position_context_into_row(row=row, position=position_context)
            await session.flush()

        monkeypatch.setattr(service, "_persist_runtime_position", _persist_runtime)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-bracket-adapter",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        kwargs = fake_adapter.place_entry_order.await_args.kwargs
        assert kwargs["take_profit_price"] == pytest.approx(102.0)
        assert kwargs["stop_loss_price"] == pytest.approx(99.0)
        assert kwargs["sl_client_order_id"] is not None
        assert kwargs["tp_client_order_id"] is not None
        # Bracket succeeded -> queue must remain empty for single-TP profile.
        assert queue.tasks == []

        position = cast(
            AutoTradePosition | None,
            await session.scalar(
                select(AutoTradePosition)
                .where(AutoTradePosition.user_id == user.id)
                .order_by(AutoTradePosition.id.desc())
                .limit(1)
            ),
        )
        assert position is not None
        assert position.status == "open"
        assert position.sl_exchange_order_id == "bracket-stop_loss-1"


async def test_signal_open_emergency_closes_when_bracket_sl_fails(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If bracket SL placement raises, the position must be flattened and not recorded as open."""
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)

        # Simulate the legacy entry call raising as if bracket SL was rejected by the exchange
        # post-fill. The trading service signals failure via `place_futures_market_order`.
        original = fake_trading.place_futures_market_order
        call_count = {"n": 0}

        async def _failing_open(**kwargs: Any) -> SpotOrderRead:
            if kwargs.get("reduce_only") is False:
                call_count["n"] += 1
                raise ExchangeServiceError(
                    code="bracket_rejected",
                    message="Stop-loss attach failed.",
                    retryable=False,
                )
            return await original(**kwargs)

        monkeypatch.setattr(fake_trading, "place_futures_market_order", _failing_open)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-bracket-fail",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)

        # The signal failed in entry placement and was retried/dead-lettered, never opened.
        assert stats["completed"] == 0
        assert call_count["n"] >= 1

        positions = list(
            (
                await session.scalars(
                    select(AutoTradePosition).where(AutoTradePosition.user_id == user.id)
                )
            ).all()
        )
        assert positions == []


async def test_signal_open_multi_tp_uses_bracket_sl_and_queue_for_tps(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-TP profile: bracket attaches SL only; multi-TP queue still drives TPs."""
    fake_trading = _FakeTradingService()
    service = AutoTradeService(
        trading_service=cast(Any, fake_trading),
        use_exchange_adapter_entry=True,
    )
    queue = _ImmediateQueue()
    fake_adapter = AsyncMock()
    fake_adapter.place_entry_order = AsyncMock(
        return_value=_build_entry_result(
            side=ExchangeOrderSide.BUY,
            attached_sl=_bracket_conditional_result(
                order_type="stop_loss", trigger_price=99.0
            ),
        )
    )

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=100.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
                strategy_profile=_strategy_profile_payload(),
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)

        monkeypatch.setattr(
            service, "_create_exchange_adapter", AsyncMock(return_value=fake_adapter)
        )
        monkeypatch.setattr(
            "app.services.auto_trade.service.get_order_queue",
            AsyncMock(return_value=queue),
        )
        monkeypatch.setattr(service, "_schedule_position_watchers", AsyncMock())
        monkeypatch.setattr(service, "_ensure_ws_manager_tracked", AsyncMock())

        async def _persist_runtime(position_context: PositionContext) -> None:
            row = await session.get(AutoTradePosition, int(position_context.position_id))
            assert row is not None
            service._merge_position_context_into_row(row=row, position=position_context)
            await session.flush()

        monkeypatch.setattr(service, "_persist_runtime_position", _persist_runtime)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-bracket-multi",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        kwargs = fake_adapter.place_entry_order.await_args.kwargs
        # Multi-TP routes TP placements through the queue, so bracket only attaches SL.
        assert kwargs["take_profit_price"] is None
        assert kwargs["stop_loss_price"] == pytest.approx(99.0)

        # Queue receives the 3 multi-TP placements but no place_sl (bracket already covered SL).
        assert [getattr(t, "action", "") for t in queue.tasks] == [
            "place_tp",
            "place_tp",
            "place_tp",
        ]

        position = cast(
            AutoTradePosition | None,
            await session.scalar(
                select(AutoTradePosition)
                .where(AutoTradePosition.user_id == user.id)
                .order_by(AutoTradePosition.id.desc())
                .limit(1)
            ),
        )
        assert position is not None
        assert position.sl_exchange_order_id == "bracket-stop_loss-1"
