import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.auto_trade.service as auto_trade_service_module
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_config_revision import AutoTradeConfigRevision
from app.models.auto_trade_event import AutoTradeEvent
from app.models.auto_trade_position import AutoTradePosition
from app.models.auto_trade_risk_config import AutoTradeRiskConfig as AutoTradeRiskConfigModel
from app.models.auto_trade_signal_queue import AutoTradeSignalQueue
from app.models.auto_trade_signal_state import AutoTradeSignalState
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.exchange_income_ledger import ExchangeIncomeLedger
from app.models.exchange_trade_ledger import ExchangeTradeLedger
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_job import PersonalAnalysisJob
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.strategy_health_snapshot import StrategyHealthSnapshot
from app.models.user import User
from app.schemas.auto_trade import AutoTradeConfigUpsertRequest, AutoTradeRiskConfig
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
from app.services.position.context import PositionContext, PositionSide
from app.services.position.state_machine import PositionState
from app.services.auto_trade.health import (
    HEALTH_MIN_TRADES,
    StrategyHealth,
    compute_strategy_health,
    get_latest_health_snapshot,
    latest_health_snapshots_for_configs,
    prune_strategy_health_snapshots,
    record_health_snapshot,
)
from app.services.auto_trade.risk import check_pre_trade
from app.services.auto_trade.service import AutoTradeService
from app.services.backtesting.common import (
    build_equity_curve,
    build_walk_forward_stability,
    calculate_equity_max_drawdown_pct,
    calculate_sharpe_proxy,
    compute_trade_r_multiple,
)
from app.services.execution.errors import ExchangeServiceError
from app.services.sl_tp.kill_switch import KillSwitchSignal
from app.services.sl_tp.live_tracker import RealtimeSLAdjuster


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

        def is_connected(self) -> bool:
            return True

        def is_reconnecting(self) -> bool:
            return False

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


async def test_multi_tp_ladder_blocks_fast_close_on_opposite_signal(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: once a multi-TP ladder has fired at least one level,
    an opposite-trend signal with confidence >= ``fast_close_confidence_pct``
    must NOT close the position. Exit is committed to the TP levels + SL.

    User-observed symptom: TP1 fires correctly, then a subsequent signal
    (or a duplicate that mis-routes) yanks the remainder at market price
    a few seconds later — defeating the multi-TP exit plan.
    """
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
            trade_job_id="job-mt-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        # Engage the multi-TP ladder: pretend TP1 has fired. The engine
        # would normally mutate ``tp_levels_json`` in place from a real
        # WS fill; here we patch it directly to exercise the gate.
        position = await service.get_open_position(session=session, user_id=user.id)
        assert position is not None
        position.tp_mode = "multi"
        position.tp_levels_json = [
            {"level": 1, "status": "triggered", "trigger_price": 105.0, "close_pct": 33.0},
            {"level": 2, "status": "open", "trigger_price": 110.0, "close_pct": 33.0},
            {"level": 3, "status": "open", "trigger_price": 115.0, "close_pct": 34.0},
        ]
        await session.commit()

        # Snapshot order-call count before the opposite signal so we can
        # assert no new close was issued.
        order_calls_before = len(fake_trading.order_calls)

        opposite = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-mt-opposite-fast",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=95.0),
        )
        await service.enqueue_history_signal(session=session, history=opposite)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        # Position must remain open.
        still_open = await service.get_open_position(session=session, user_id=user.id)
        assert still_open is not None
        assert still_open.status == "open"
        # No additional order calls (only the original entry).
        assert len(fake_trading.order_calls) == order_calls_before

        # Audit event recorded.
        events = list(
            (
                await session.scalars(
                    select(AutoTradeEvent).where(
                        AutoTradeEvent.position_id == position.id,
                        AutoTradeEvent.event_type
                        == "opposite_signal_ignored_multi_tp_engaged",
                    )
                )
            ).all()
        )
        assert events, "expected an opposite_signal_ignored_multi_tp_engaged audit event"
        assert events[0].payload["triggered_tp_levels"] == [1]


async def test_multi_tp_ladder_blocks_streak_close_on_opposite_signal(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Streak path is also gated by the ladder lock: even if N opposite
    confirmations arrive (each below fast-close confidence), they must
    not close a multi-TP position whose ladder is engaged.
    """
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
            trade_job_id="job-mt-streak-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        position = await service.get_open_position(session=session, user_id=user.id)
        assert position is not None
        position.tp_mode = "multi"
        position.tp_levels_json = [
            {"level": 1, "status": "triggered", "trigger_price": 105.0, "close_pct": 33.0},
            {"level": 2, "status": "open", "trigger_price": 110.0, "close_pct": 33.0},
            {"level": 3, "status": "open", "trigger_price": 115.0, "close_pct": 34.0},
        ]
        await session.commit()

        order_calls_before = len(fake_trading.order_calls)

        # Two opposite signals below fast-close threshold — would normally
        # trip ``confirm_reports_required == 2``.
        for index in range(2):
            history = await _create_history(
                session,
                user_id=user.id,
                profile_id=profile.id,
                trade_job_id=f"job-mt-streak-{index}",
                signal_payload=_build_signal(trend="SHORT", confidence_pct=65.0),
            )
            await service.enqueue_history_signal(session=session, history=history)
            await service.process_signal_queue(session=session)

        still_open = await service.get_open_position(session=session, user_id=user.id)
        assert still_open is not None
        assert still_open.status == "open"
        assert len(fake_trading.order_calls) == order_calls_before


async def test_single_tp_position_still_closes_on_opposite_fast_signal(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard: the ladder lock must NOT affect single-TP
    positions — those still close on a fast-confidence opposite signal
    as they always have. Only multi-TP positions with at least one
    triggered level get the lock.
    """
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
            trade_job_id="job-single-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        # Single-TP position by default. No mutation of tp_levels_json.
        position = await service.get_open_position(session=session, user_id=user.id)
        assert position is not None
        assert position.tp_mode == "single"

        opposite = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-single-fast",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=90.0),
        )
        await service.enqueue_history_signal(session=session, history=opposite)
        await service.process_signal_queue(session=session)

        closed_position = await service.get_open_position(session=session, user_id=user.id)
        assert closed_position is None  # closed


async def test_multi_tp_position_without_triggered_levels_still_fast_closes(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard: a multi-TP position whose ladder has NOT yet
    fired any level is NOT locked. An opposite fast-confidence signal
    can still close it — the lock activates only after the ladder is
    engaged, because before that point the user hasn't yet locked in
    any profit and an opposite signal genuinely indicates trend reversal.
    """
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
            trade_job_id="job-mt-not-engaged-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=open_history)
        await service.process_signal_queue(session=session)

        position = await service.get_open_position(session=session, user_id=user.id)
        assert position is not None
        # Multi-TP ladder configured but no level fired yet.
        position.tp_mode = "multi"
        position.tp_levels_json = [
            {"level": 1, "status": "open", "trigger_price": 105.0, "close_pct": 33.0},
            {"level": 2, "status": "open", "trigger_price": 110.0, "close_pct": 33.0},
            {"level": 3, "status": "open", "trigger_price": 115.0, "close_pct": 34.0},
        ]
        await session.commit()

        opposite = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-mt-not-engaged-fast",
            signal_payload=_build_signal(trend="SHORT", confidence_pct=90.0),
        )
        await service.enqueue_history_signal(session=session, history=opposite)
        await service.process_signal_queue(session=session)

        closed_position = await service.get_open_position(session=session, user_id=user.id)
        assert closed_position is None  # closed — lock not engaged, fast-close fires


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


def _make_closed_position(
    *,
    user_id: int,
    config_id: int,
    profile_id: int,
    account_id: int,
    idx: int,
    closed_at: datetime,
    close_price: float = 101.0,
    side: str = "LONG",
) -> AutoTradePosition:
    return AutoTradePosition(
        user_id=user_id,
        config_id=config_id,
        profile_id=profile_id,
        account_id=account_id,
        symbol="BTC/USDT:USDT",
        side=side,
        status="closed",
        entry_price=100.0,
        quantity=1.0,
        position_size_usdt=100.0,
        leverage=1,
        tp_price=102.0,
        sl_price=99.0,
        entry_confidence_pct=70.0,
        opened_at=closed_at - timedelta(hours=1),
        closed_at=closed_at,
        close_reason="tp",
        close_price=close_price,
        open_order_id=f"o{idx}",
        close_order_id=f"c{idx}",
        open_history_id=None,
        close_history_id=None,
        raw_open_order={},
        raw_close_order={},
    )


async def test_list_positions_filters_by_closed_at_window(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T0.1 — closed_after/closed_before narrow closed positions by ``closed_at``.

    ``closed_after`` is an inclusive lower bound, ``closed_before`` an exclusive
    upper bound — exactly the ``[start_of_day, start_of_next_day)`` shape the
    daily-loss rule (T1.5) and the health window (T2.1) need.
    """

    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async def _noop_snapshot(**_: object) -> None:
        return None

    # Keep the query pure: no exchange round-trip on read.
    monkeypatch.setattr(service, "_sync_positions_snapshot_for_user", _noop_snapshot)

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = AutoTradeConfig(
            user_id=user.id,
            profile_id=profile.id,
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
        )
        session.add(config)
        await session.flush()

        base = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        old = _make_closed_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            idx=0,
            closed_at=base - timedelta(days=2),
        )
        mid = _make_closed_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            idx=1,
            closed_at=base - timedelta(days=1),
        )
        recent = _make_closed_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            idx=2,
            closed_at=base,
        )
        session.add_all([old, mid, recent])
        await session.commit()

        # No window → every closed position is returned (back-compat).
        all_closed = await service.list_positions(
            session=session,
            user_id=user.id,
            limit=50,
            status="closed",
            config_id=config.id,
        )
        assert {p.id for p in all_closed} == {old.id, mid.id, recent.id}

        # Inclusive lower bound keeps mid (== bound) and recent.
        windowed = await service.list_positions(
            session=session,
            user_id=user.id,
            limit=50,
            status="closed",
            config_id=config.id,
            closed_after=base - timedelta(days=1),
        )
        assert {p.id for p in windowed} == {mid.id, recent.id}

        # Exclusive upper bound drops recent (== bound).
        before = await service.list_positions(
            session=session,
            user_id=user.id,
            limit=50,
            status="closed",
            config_id=config.id,
            closed_before=base,
        )
        assert {p.id for p in before} == {old.id, mid.id}

        # summarize_positions_pnl honours the same window.
        summary = await service.summarize_positions_pnl(
            session=session,
            user_id=user.id,
            limit=50,
            status="closed",
            config_id=config.id,
            closed_after=base - timedelta(days=1),
        )
        assert summary["summary"]["closed_positions"] == 2


def test_risk_config_schema_rejects_invalid_conflicting_signal_policy() -> None:
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(conflicting_signal_policy="bogus")


def test_risk_config_schema_rejects_out_of_range_limits() -> None:
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(daily_loss_limit_usdt=-1.0)
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(max_open_positions=0)
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(exposure_cap_usdt=0.0)


def _upsert_payload_with_risk(
    *,
    profile_id: int,
    account_id: int,
    risk: AutoTradeRiskConfig | None,
) -> AutoTradeConfigUpsertRequest:
    return AutoTradeConfigUpsertRequest(
        enabled=True,
        profile_id=profile_id,
        account_id=account_id,
        position_size_usdt=100.0,
        leverage=3,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
        risk=risk,
    )


async def test_upsert_config_persists_and_returns_risk_config(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.1 — upsert persists a 1:1 risk row; updates replace it wholesale."""

    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)

        created = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_upsert_payload_with_risk(
                profile_id=profile.id,
                account_id=account_id,
                risk=AutoTradeRiskConfig(
                    daily_loss_limit_usdt=50.0,
                    max_open_positions=3,
                    exposure_cap_usdt=500.0,
                    leverage_ceiling=5,
                ),
            ),
        )

        risk = await service.get_risk_config(session=session, config_id=created.id)
        assert risk is not None
        assert risk.enabled is True  # column default
        assert risk.daily_loss_limit_usdt == 50.0
        assert risk.max_open_positions == 3
        assert risk.exposure_cap_usdt == 500.0
        assert risk.leverage_ceiling == 5
        assert risk.conflicting_signal_policy == "off"  # schema default — conflict blocking is opt-in
        assert risk.daily_loss_limit_pct is None

        # Re-upsert with a different (smaller) risk set — replace, do not stack.
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_upsert_payload_with_risk(
                profile_id=profile.id,
                account_id=account_id,
                risk=AutoTradeRiskConfig(
                    daily_loss_limit_usdt=10.0,
                    conflicting_signal_policy="block_opposite",
                ),
            ),
        )
        updated = await service.get_risk_config(session=session, config_id=created.id)
        assert updated is not None
        assert updated.daily_loss_limit_usdt == 10.0
        assert updated.conflicting_signal_policy == "block_opposite"
        assert updated.max_open_positions is None  # wholesale replace
        assert updated.leverage_ceiling is None

        row_count = await session.scalar(select(func.count()).select_from(AutoTradeRiskConfigModel))
        assert row_count == 1


async def test_upsert_config_without_risk_leaves_no_risk_row(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Omitting ``risk`` is fail-safe: no row, engine treats every limit as off."""

    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        created = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_upsert_payload_with_risk(
                profile_id=profile.id,
                account_id=account_id,
                risk=None,
            ),
        )
        assert await service.get_risk_config(session=session, config_id=created.id) is None


def _upsert_payload(
    *,
    profile_id: int,
    account_id: int,
    leverage: int,
    risk: AutoTradeRiskConfig | None,
) -> AutoTradeConfigUpsertRequest:
    return AutoTradeConfigUpsertRequest(
        enabled=True,
        profile_id=profile_id,
        account_id=account_id,
        position_size_usdt=100.0,
        leverage=leverage,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
        risk=risk,
    )


async def test_pre_trade_risk_gate_blocks_on_leverage_ceiling(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1.2 — leverage above the ceiling blocks the entry with a risk_blocked event."""

    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))
    # Defensive: the gate returns before any order, but if it failed to block
    # these keep the would-open path from touching Redis/WS in the test.
    monkeypatch.setattr(service, "_schedule_position_watchers", AsyncMock(return_value="w"))
    monkeypatch.setattr(service, "_ensure_ws_manager_tracked", AsyncMock())

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_upsert_payload(
                profile_id=profile.id,
                account_id=account_id,
                leverage=10,
                risk=AutoTradeRiskConfig(leverage_ceiling=5),
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-risk-leverage-block",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        position = await session.scalar(
            select(AutoTradePosition).where(AutoTradePosition.user_id == user.id)
        )
        assert position is None  # blocked before any order

        event = await session.scalar(
            select(AutoTradeEvent).where(AutoTradeEvent.event_type == "risk_blocked")
        )
        assert event is not None
        assert event.payload["rule"] == "leverage"
        assert event.payload["leverage"] == 10
        assert event.payload["leverage_ceiling"] == 5


async def test_pre_trade_risk_gate_noop_without_risk_config(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1.2 regression — no risk config ⇒ gate is a no-op, the entry opens as before."""

    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))
    monkeypatch.setattr(service, "_schedule_position_watchers", AsyncMock(return_value="w"))
    monkeypatch.setattr(service, "_ensure_ws_manager_tracked", AsyncMock())

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_upsert_payload(
                profile_id=profile.id,
                account_id=account_id,
                leverage=10,  # high leverage, but no ceiling to enforce
                risk=None,
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-risk-noop",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        position = await session.scalar(
            select(AutoTradePosition).where(AutoTradePosition.user_id == user.id)
        )
        assert position is not None
        assert position.status == "open"

        blocked = await session.scalar(
            select(AutoTradeEvent).where(AutoTradeEvent.event_type == "risk_blocked")
        )
        assert blocked is None


def _make_open_position(
    *,
    user_id: int,
    config_id: int,
    profile_id: int,
    account_id: int,
    symbol: str,
    idx: int,
    position_size_usdt: float = 100.0,
    side: str = "LONG",
) -> AutoTradePosition:
    return AutoTradePosition(
        user_id=user_id,
        config_id=config_id,
        profile_id=profile_id,
        account_id=account_id,
        symbol=symbol,
        side=side,
        status="open",
        entry_price=100.0,
        quantity=1.0,
        position_size_usdt=position_size_usdt,
        leverage=1,
        tp_price=102.0,
        sl_price=99.0,
        entry_confidence_pct=70.0,
        opened_at=datetime.now(UTC),
        closed_at=None,
        close_reason=None,
        close_price=None,
        open_order_id=f"o-open-{idx}",
        close_order_id=None,
        open_history_id=None,
        close_history_id=None,
        raw_open_order={},
        raw_close_order={},
    )


async def _insert_config(
    session: AsyncSession,
    *,
    user_id: int,
    profile_id: int,
    account_id: int,
    leverage: int = 1,
) -> AutoTradeConfig:
    config = AutoTradeConfig(
        user_id=user_id,
        profile_id=profile_id,
        account_id=account_id,
        enabled=True,
        is_running=False,
        position_size_usdt=100.0,
        leverage=leverage,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
    )
    session.add(config)
    await session.flush()
    return config


async def test_pre_trade_risk_max_open_positions_counts_across_user(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.3 — ``max_open_positions`` caps the user's TOTAL concurrent positions.

    Per-config is degenerate (the ``(user_id, account_id) WHERE status='open'``
    unique index caps an account at one open position, and the gate only runs
    when this config has none), so the rule counts across all the user's
    strategies — a portfolio-wide concurrency cap.
    """

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        session.add(
            _make_open_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                symbol="BTC/USDT:USDT",
                idx=0,
            )
        )
        await session.commit()

        blocked = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True, max_open_positions=1),
            signal=cast(Any, None),
            execution_symbol="ETH/USDT:USDT",
        )
        assert blocked.allowed is False
        assert blocked.rule == "max_open"
        assert blocked.payload["open_positions"] == 1
        assert blocked.payload["max_open_positions"] == 1

        allowed = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True, max_open_positions=2),
            signal=cast(Any, None),
            execution_symbol="ETH/USDT:USDT",
        )
        assert allowed.allowed is True


async def test_pre_trade_risk_max_open_per_symbol_independent(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.3 — ``max_open_positions_per_symbol`` is symbol-scoped and independent."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        session.add(
            _make_open_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                symbol="BTC/USDT:USDT",
                idx=0,
            )
        )
        await session.commit()

        # Same symbol, cap reached → block.
        blocked = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True, max_open_positions_per_symbol=1),
            signal=cast(Any, None),
            execution_symbol="BTC/USDT:USDT",
        )
        assert blocked.allowed is False
        assert blocked.rule == "max_open_per_symbol"
        assert blocked.payload["symbol"] == "BTC/USDT:USDT"
        assert blocked.payload["open_positions_symbol"] == 1

        # Different symbol → that symbol has 0 open → allow, even though the
        # global open count is 1 (global cap is unset here).
        allowed = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True, max_open_positions_per_symbol=1),
            signal=cast(Any, None),
            execution_symbol="ETH/USDT:USDT",
        )
        assert allowed.allowed is True


async def test_pre_trade_risk_max_open_none_skips(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Both caps unset ⇒ rule is skipped even with open positions present."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        session.add(
            _make_open_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                symbol="BTC/USDT:USDT",
                idx=0,
            )
        )
        await session.commit()

        decision = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True),
            signal=cast(Any, None),
            execution_symbol="BTC/USDT:USDT",
        )
        assert decision.allowed is True


async def _seed_open_position_for_exposure(
    session: AsyncSession,
    *,
    user_id: int,
    profile_id: int,
    account_id: int,
    margin_usdt: float,
) -> AutoTradeConfig:
    config = await _insert_config(
        session, user_id=user_id, profile_id=profile_id, account_id=account_id
    )
    session.add(
        _make_open_position(
            user_id=user_id,
            config_id=config.id,
            profile_id=profile_id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            idx=0,
            position_size_usdt=margin_usdt,
        )
    )
    await session.commit()
    return config


async def test_pre_trade_risk_exposure_cap_blocks_when_projected_exceeds(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.4 — current exposure + the new entry's margin over the cap ⇒ block."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        # 300 USDT already deployed across the user's strategies.
        config = await _seed_open_position_for_exposure(
            session,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
            margin_usdt=300.0,
        )
        # config.position_size_usdt == 100 (the would-be new entry) → projected 400.
        blocked = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True, exposure_cap_usdt=350.0),
            signal=cast(Any, None),
            execution_symbol="BTC/USDT:USDT",
        )
        assert blocked.allowed is False
        assert blocked.rule == "exposure"
        assert blocked.payload["current_exposure_usdt"] == 300.0
        assert blocked.payload["new_position_size_usdt"] == 100.0
        assert blocked.payload["projected_exposure_usdt"] == 400.0
        assert blocked.payload["exposure_cap_usdt"] == 350.0


async def test_pre_trade_risk_exposure_cap_allows_at_or_below(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """The cap is inclusive: projected exposure exactly at / below the cap is allowed."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _seed_open_position_for_exposure(
            session,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
            margin_usdt=300.0,
        )
        # projected 400 == cap → allowed (inclusive).
        at_cap = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True, exposure_cap_usdt=400.0),
            signal=cast(Any, None),
            execution_symbol="BTC/USDT:USDT",
        )
        assert at_cap.allowed is True
        # projected 400 < 500 → allowed.
        below_cap = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True, exposure_cap_usdt=500.0),
            signal=cast(Any, None),
            execution_symbol="BTC/USDT:USDT",
        )
        assert below_cap.allowed is True


async def test_pre_trade_risk_exposure_cap_none_skips(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """No ``exposure_cap_usdt`` ⇒ rule skipped even with large open exposure."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _seed_open_position_for_exposure(
            session,
            user_id=user.id,
            profile_id=profile.id,
            account_id=account_id,
            margin_usdt=10_000.0,
        )
        decision = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True),
            signal=cast(Any, None),
            execution_symbol="BTC/USDT:USDT",
        )
        assert decision.allowed is True


async def test_pre_trade_risk_gate_blocks_on_max_open_across_configs(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1.3 end-to-end — a position open under strategy B blocks strategy A's entry."""

    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))
    monkeypatch.setattr(service, "_schedule_position_watchers", AsyncMock(return_value="w"))
    monkeypatch.setattr(service, "_ensure_ws_manager_tracked", AsyncMock())

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        # Strategy B (other account) already holds an open position.
        profile_b, account_b_id = await _create_profile_and_account(
            session, user_id=user.id, symbol="ETHUSDT", account_label="strategy-b"
        )
        config_b = await _insert_config(
            session, user_id=user.id, profile_id=profile_b.id, account_id=account_b_id
        )
        session.add(
            _make_open_position(
                user_id=user.id,
                config_id=config_b.id,
                profile_id=profile_b.id,
                account_id=account_b_id,
                symbol="ETH/USDT:USDT",
                idx=0,
            )
        )
        await session.commit()

        # Strategy A: portfolio cap of 1 concurrent position for the user.
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_upsert_payload(
                profile_id=profile.id,
                account_id=account_id,
                leverage=1,
                risk=AutoTradeRiskConfig(max_open_positions=1),
            ),
        )
        await service.set_running(
            session=session, user_id=user.id, is_running=True, account_id=account_id
        )

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-risk-max-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        await service.process_signal_queue(session=session)

        # No new position opened on strategy A's account.
        a_position = await session.scalar(
            select(AutoTradePosition).where(AutoTradePosition.account_id == account_id)
        )
        assert a_position is None

        event = await session.scalar(
            select(AutoTradeEvent).where(AutoTradeEvent.event_type == "risk_blocked")
        )
        assert event is not None
        assert event.payload["rule"] == "max_open"


async def test_pre_trade_risk_conflicting_signal_blocks_opposite(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.6 — an open opposite-side position on the same symbol blocks the entry.

    The check is user + symbol scoped (a config owns exactly one account, so the
    conflicting position belongs to another strategy of the same user).
    """

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        session.add(
            _make_open_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                symbol="BTC/USDT:USDT",
                idx=0,
                side="SHORT",
            )
        )
        await session.commit()

        decision = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(
                enabled=True, conflicting_signal_policy="block_opposite"
            ),
            signal=cast(Any, SimpleNamespace(trend="LONG")),
            execution_symbol="BTC/USDT:USDT",
        )
        assert decision.allowed is False
        assert decision.rule == "conflicting_signal"
        assert decision.payload["intended_side"] == "LONG"
        assert decision.payload["open_opposite_side"] == "SHORT"


async def test_pre_trade_risk_conflicting_signal_allows_same_side(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Same-side open position is not a conflict — allowed."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        session.add(
            _make_open_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                symbol="BTC/USDT:USDT",
                idx=0,
                side="LONG",
            )
        )
        await session.commit()

        decision = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(
                enabled=True, conflicting_signal_policy="block_opposite"
            ),
            signal=cast(Any, SimpleNamespace(trend="LONG")),
            execution_symbol="BTC/USDT:USDT",
        )
        assert decision.allowed is True


async def test_pre_trade_risk_conflicting_signal_ignores_other_symbol(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """An opposite position on a different symbol does not conflict."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        session.add(
            _make_open_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                symbol="ETH/USDT:USDT",
                idx=0,
                side="SHORT",
            )
        )
        await session.commit()

        decision = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(
                enabled=True, conflicting_signal_policy="block_opposite"
            ),
            signal=cast(Any, SimpleNamespace(trend="LONG")),
            execution_symbol="BTC/USDT:USDT",
        )
        assert decision.allowed is True


async def test_today_realized_pnl_respects_utc_day_boundary(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.5 — only positions closed within the current UTC day count toward daily loss."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        # Closed today, LONG entry 100 → close 90 ⇒ realized -10.
        session.add(
            _make_closed_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                idx=0,
                closed_at=day_start + timedelta(minutes=1),
                close_price=90.0,
            )
        )
        # Closed yesterday 23:59, close 80 ⇒ realized -20, must be EXCLUDED.
        session.add(
            _make_closed_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                idx=1,
                closed_at=day_start - timedelta(minutes=1),
                close_price=80.0,
            )
        )
        await session.commit()

        total = await service._today_realized_pnl_usdt(session=session, config_id=config.id)
        assert total == pytest.approx(-10.0)


async def test_today_realized_pnl_computed_in_sql_without_exchange_snapshot(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 fix — daily-loss PnL is a single SQL aggregate, never the exchange-touching snapshot."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))

    async def _boom(**_: object) -> dict[str, Any]:
        raise AssertionError(
            "daily-loss path must not call build_position_pnl_snapshot (it hits the exchange)"
        )

    monkeypatch.setattr(service, "build_position_pnl_snapshot", _boom)

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        # LONG entry 100 → close 90 ⇒ -10 ; SHORT entry 100 → close 110 ⇒ -10.
        session.add(
            _make_closed_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                idx=0,
                closed_at=day_start + timedelta(minutes=1),
                close_price=90.0,
                side="LONG",
            )
        )
        session.add(
            _make_closed_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                idx=1,
                closed_at=day_start + timedelta(minutes=2),
                close_price=110.0,
                side="SHORT",
            )
        )
        # Closed yesterday — excluded.
        session.add(
            _make_closed_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                idx=2,
                closed_at=day_start - timedelta(minutes=1),
                close_price=50.0,
                side="LONG",
            )
        )
        await session.commit()

        total = await service._today_realized_pnl_usdt(session=session, config_id=config.id)
        assert total == pytest.approx(-20.0)


async def test_pre_trade_gate_skips_balance_fetch_without_a_loss(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I1 fix — with daily_loss_pct set but no loss today, the balance is never fetched."""

    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))
    monkeypatch.setattr(service, "_schedule_position_watchers", AsyncMock(return_value="w"))
    monkeypatch.setattr(service, "_ensure_ws_manager_tracked", AsyncMock())
    balance_mock = AsyncMock(return_value=1000.0)
    monkeypatch.setattr(service, "_safe_subaccount_usdt_balance", balance_mock)

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_upsert_payload(
                profile_id=profile.id,
                account_id=account_id,
                leverage=1,
                risk=AutoTradeRiskConfig(daily_loss_limit_pct=5.0),
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)

        # No positions closed today ⇒ today_realized == 0 ⇒ pct rule moot.
        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-no-loss",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        await service.process_signal_queue(session=session)

        balance_mock.assert_not_awaited()
        degraded = await session.scalar(
            select(AutoTradeEvent).where(AutoTradeEvent.event_type == "risk_check_degraded")
        )
        assert degraded is None
        position = await session.scalar(
            select(AutoTradePosition).where(AutoTradePosition.account_id == account_id)
        )
        assert position is not None


async def test_pre_trade_risk_gate_fail_open_on_balance_error(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1.5 — with a loss today, a failed balance fetch never blocks: pct skipped, warned, opens."""

    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))
    monkeypatch.setattr(service, "_schedule_position_watchers", AsyncMock(return_value="w"))
    monkeypatch.setattr(service, "_ensure_ws_manager_tracked", AsyncMock())
    # A flaky exchange (expected error) → _safe_subaccount_usdt_balance fails open
    # (None). raising=False: _FakeTradingService has no get_spot_balances to begin with.
    monkeypatch.setattr(
        service._trading,
        "get_spot_balances",
        AsyncMock(side_effect=TimeoutError("exchange down")),
        raising=False,
    )

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_upsert_payload(
                profile_id=profile.id,
                account_id=account_id,
                leverage=1,
                risk=AutoTradeRiskConfig(daily_loss_limit_pct=5.0),
            ),
        )
        await service.set_running(session=session, user_id=user.id, is_running=True)
        # A realized loss today so the pct rule is live and the balance fetch is
        # attempted (and fails) — otherwise the fetch is correctly skipped (I1).
        session.add(
            _make_closed_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                idx=0,
                closed_at=datetime.now(UTC),
                close_price=90.0,  # LONG entry 100 → -10
            )
        )
        await session.commit()

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-risk-fail-open",
            signal_payload=_build_signal(trend="LONG", confidence_pct=70.0),
        )
        await service.enqueue_history_signal(session=session, history=history)
        await service.process_signal_queue(session=session)

        # Degraded warning emitted, trade NOT blocked.
        warning = await session.scalar(
            select(AutoTradeEvent).where(AutoTradeEvent.event_type == "risk_check_degraded")
        )
        assert warning is not None
        blocked = await session.scalar(
            select(AutoTradeEvent).where(AutoTradeEvent.event_type == "risk_blocked")
        )
        assert blocked is None
        position = await session.scalar(
            select(AutoTradePosition).where(
                AutoTradePosition.account_id == account_id,
                AutoTradePosition.status == "open",
            )
        )
        assert position is not None
        assert position.status == "open"


async def _seed_closed_positions(
    session: AsyncSession,
    *,
    user_id: int,
    config_id: int,
    profile_id: int,
    account_id: int,
    close_prices: list[float],
    base: datetime,
) -> None:
    for idx, price in enumerate(close_prices):
        session.add(
            _make_closed_position(
                user_id=user_id,
                config_id=config_id,
                profile_id=profile_id,
                account_id=account_id,
                idx=idx,
                closed_at=base + timedelta(minutes=idx),
                close_price=price,
            )
        )
    await session.commit()


async def test_strategy_health_matches_direct_metric_calls(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T2.1 golden set — health metrics reconcile with direct backtesting/common.py calls."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        # [win, win, loss] x4 → 8 wins (close 110, +10) + 4 losses (close 90, -10).
        # LONG entry 100, sl 99 ⇒ risk 1 ⇒ R-multiple == pnl.
        close_prices = [110.0, 110.0, 90.0] * 4
        base = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
        await _seed_closed_positions(
            session,
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            close_prices=close_prices,
            base=base,
        )

        health = await compute_strategy_health(
            session=session, config_id=config.id, window_days=30
        )

        # Reconstruct the metric inputs exactly as the health service does.
        trades = [
            {
                "pnl_usdt": price - 100.0,
                "exit_reason": "closed",
                "exit_time": None,
                "entry": 100.0,
                "sl": 99.0,
                "position_size": 1.0,
            }
            for price in close_prices
        ]
        r_values = [r for t in trades if (r := compute_trade_r_multiple(t)) is not None]
        equity = build_equity_curve(trades, float(config.position_size_usdt))

        assert health.sample_size == 12
        assert health.win_rate_pct == pytest.approx(8 / 12 * 100.0)
        assert health.total_pnl_usdt == pytest.approx(40.0)
        assert health.sharpe_proxy == pytest.approx(calculate_sharpe_proxy(r_values))
        assert health.stability_score == pytest.approx(
            build_walk_forward_stability(r_values)["stability_score"]
        )
        assert health.max_dd_pct == pytest.approx(calculate_equity_max_drawdown_pct(equity))
        # roi_pct (AC#7 / W9 T0.1): total realized PnL as % of the normalization base.
        assert health.roi_pct == pytest.approx(
            health.total_pnl_usdt / float(config.position_size_usdt) * 100.0
        )
        assert 0.0 <= health.health_score <= 100.0
        assert health.health_class in {"healthy", "warning", "critical"}


async def test_strategy_health_insufficient_data_below_min_trades(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Fewer than HEALTH_MIN_TRADES closed trades ⇒ insufficient_data, never a false critical."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        base = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
        await _seed_closed_positions(
            session,
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            close_prices=[90.0] * (HEALTH_MIN_TRADES - 1),  # all losses, but too few to judge
            base=base,
        )

        health = await compute_strategy_health(
            session=session, config_id=config.id, window_days=30
        )
        assert health.sample_size == HEALTH_MIN_TRADES - 1
        assert health.health_class == "insufficient_data"
        assert health.health_score == 0.0
        # roi_pct is a raw return ratio (like total_pnl_usdt), populated even on the
        # insufficient_data path — only the noisy statistical metrics are zeroed.
        assert health.roi_pct == pytest.approx(
            health.total_pnl_usdt / float(config.position_size_usdt) * 100.0
        )


async def test_strategy_health_window_excludes_old_trades(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Only trades closed within window_days are counted."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        now = datetime.now(UTC)
        # 12 recent (in window) + 3 old (45 days ago, outside a 30-day window).
        await _seed_closed_positions(
            session,
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            close_prices=[110.0] * 12,
            base=now - timedelta(days=1),
        )
        await _seed_closed_positions(
            session,
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            close_prices=[90.0] * 3,
            base=now - timedelta(days=45),
        )

        health = await compute_strategy_health(
            session=session, config_id=config.id, window_days=30
        )
        assert health.sample_size == 12


async def test_strategy_health_empty_is_safe(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """No closed positions ⇒ zeros + insufficient_data, no exception."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        health = await compute_strategy_health(
            session=session, config_id=config.id, window_days=30
        )
        assert health.sample_size == 0
        assert health.health_class == "insufficient_data"
        assert health.health_score == 0.0
        assert health.total_pnl_usdt == 0.0
        assert health.roi_pct == 0.0


async def test_record_and_read_latest_health_snapshot_round_trip(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T0.2 — a recorded snapshot persists every KPI field and is the 'latest per config' read."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        await _seed_closed_positions(
            session,
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            close_prices=[110.0, 110.0, 90.0] * 4,
            base=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
        )
        health = await compute_strategy_health(
            session=session, config_id=config.id, window_days=30
        )

        saved = await record_health_snapshot(session=session, health=health, user_id=user.id)
        assert saved.id is not None

        latest = await get_latest_health_snapshot(session=session, config_id=config.id)
        assert latest is not None
        assert latest.config_id == config.id
        assert latest.user_id == user.id
        assert latest.window_days == health.window_days
        assert latest.sample_size == health.sample_size
        assert latest.win_rate_pct == pytest.approx(health.win_rate_pct)
        assert latest.max_dd_pct == pytest.approx(health.max_dd_pct)
        assert latest.total_pnl_usdt == pytest.approx(health.total_pnl_usdt)
        assert latest.roi_pct == pytest.approx(health.roi_pct)
        assert latest.sharpe_proxy == pytest.approx(health.sharpe_proxy)
        assert latest.stability_score == pytest.approx(health.stability_score)
        assert latest.health_score == pytest.approx(health.health_score)
        assert latest.health_class == health.health_class
        assert latest.computed_at is not None


async def test_health_snapshots_are_append_only_latest_wins(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T0.2 — append-only series: repeated writes never collide (W8 I7); latest = newest."""

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )

        def _mk(computed_at: datetime, score: float) -> StrategyHealth:
            return StrategyHealth(
                config_id=config.id,
                window_days=30,
                sample_size=12,
                win_rate_pct=50.0,
                max_dd_pct=10.0,
                total_pnl_usdt=5.0,
                roi_pct=5.0,
                sharpe_proxy=1.0,
                stability_score=0.5,
                health_score=score,
                health_class="warning",
                computed_at=computed_at,
            )

        older = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
        newer = datetime(2026, 6, 1, 4, 0, tzinfo=UTC)
        await record_health_snapshot(session=session, health=_mk(older, 40.0), user_id=user.id)
        await record_health_snapshot(session=session, health=_mk(newer, 80.0), user_id=user.id)

        rows = (
            await session.scalars(
                select(StrategyHealthSnapshot).where(
                    StrategyHealthSnapshot.config_id == config.id
                )
            )
        ).all()
        assert len(rows) == 2  # append-only — no upsert collision crash

        latest = await get_latest_health_snapshot(session=session, config_id=config.id)
        assert latest is not None
        assert latest.health_score == pytest.approx(80.0)  # newest computed_at wins


async def test_kpi_guard_apply_pauses_running_strategy_and_is_idempotent(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.2 — a max_dd breach auto-pauses a RUNNING strategy once, emitting both
    events; a second apply on the now-paused strategy is a clean no-op."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config.id,
                enabled=True,
                kpi_guard_enabled=True,
                kpi_guard_max_dd_pct=20.0,
                kpi_guard_min_trades=10,
                conflicting_signal_policy="off",
            )
        )
        await session.commit()

        breaching = StrategyHealth(
            config_id=config.id,
            window_days=30,
            sample_size=12,
            win_rate_pct=30.0,
            max_dd_pct=40.0,  # > 20.0 threshold
            total_pnl_usdt=-50.0,
            roi_pct=-50.0,
            sharpe_proxy=-1.0,
            stability_score=0.2,
            health_score=25.0,
            health_class="critical",
            computed_at=datetime.now(UTC),
        )

        decision = await service.apply_kpi_guard(
            session=session, config_id=config.id, health=breaching
        )
        assert decision.should_pause is True

        await session.refresh(config)
        assert config.is_running is False

        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config.id)
            )
        ).all()
        types = [event.event_type for event in events]
        assert "kpi_guard_triggered" in types
        assert "strategy_auto_paused" in types
        first_count = len(events)

        # Idempotent: applying again on the now-paused strategy emits nothing new.
        await service.apply_kpi_guard(session=session, config_id=config.id, health=breaching)
        events_after = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config.id)
            )
        ).all()
        assert len(events_after) == first_count


async def test_kpi_guard_apply_within_limit_leaves_strategy_running(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.2 — a healthy strategy is untouched: no pause, no events."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config.id,
                enabled=True,
                kpi_guard_enabled=True,
                kpi_guard_max_dd_pct=50.0,
                kpi_guard_min_trades=10,
                conflicting_signal_policy="off",
            )
        )
        await session.commit()

        healthy = StrategyHealth(
            config_id=config.id,
            window_days=30,
            sample_size=12,
            win_rate_pct=70.0,
            max_dd_pct=10.0,  # < 50.0 threshold
            total_pnl_usdt=80.0,
            roi_pct=80.0,
            sharpe_proxy=1.5,
            stability_score=0.8,
            health_score=85.0,
            health_class="healthy",
            computed_at=datetime.now(UTC),
        )

        decision = await service.apply_kpi_guard(
            session=session, config_id=config.id, health=healthy
        )
        assert decision.should_pause is False

        await session.refresh(config)
        assert config.is_running is True
        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config.id)
            )
        ).all()
        assert events == []


async def test_sweep_kpi_guards_pauses_breaching_and_skips_paused(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.3 — the sweep pauses a RUNNING breaching strategy (recording a snapshot),
    then on the next tick skips it because it is no longer running."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config.id,
                enabled=True,
                kpi_guard_enabled=True,
                kpi_guard_max_dd_pct=20.0,
                kpi_guard_min_trades=10,
                conflicting_signal_policy="off",
            )
        )
        # 6 wins then 6 losses ⇒ a deep equity drawdown (~75%) over 12 trades.
        await _seed_closed_positions(
            session,
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            close_prices=[110.0] * 6 + [80.0] * 6,
            base=datetime.now(UTC) - timedelta(hours=12),
        )

        stats1 = await service.sweep_kpi_guards(session=session)
        assert stats1["evaluated"] == 1
        assert stats1["paused"] == 1
        assert stats1["errors"] == 0

        await session.refresh(config)
        assert config.is_running is False
        snaps = (
            await session.scalars(
                select(StrategyHealthSnapshot).where(
                    StrategyHealthSnapshot.config_id == config.id
                )
            )
        ).all()
        assert len(snaps) == 1  # one snapshot recorded for the tick

        # Next tick: the config is no longer running ⇒ skipped, no new snapshot.
        stats2 = await service.sweep_kpi_guards(session=session)
        assert stats2["evaluated"] == 0
        snaps2 = (
            await session.scalars(
                select(StrategyHealthSnapshot).where(
                    StrategyHealthSnapshot.config_id == config.id
                )
            )
        ).all()
        assert len(snaps2) == 1  # unchanged — skipped


async def test_on_close_hook_pauses_on_daily_loss_and_is_idempotent(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T1.4 — the on-close fast-path pauses on a same-day realized-loss breach,
    even below the statistical sample floor (daily-loss is a hard stop); a second
    call on the now-paused strategy is a clean no-op."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config.id,
                enabled=True,
                kpi_guard_enabled=True,
                kpi_guard_max_daily_loss_usdt=30.0,
                conflicting_signal_policy="off",
            )
        )
        # 3 losing closes TODAY: (80-100)*1 = -20 each ⇒ -60 net (> 30 limit),
        # only 3 trades (below any statistical floor) — daily-loss still fires.
        await _seed_closed_positions(
            session,
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            close_prices=[80.0, 80.0, 80.0],
            base=datetime.now(UTC),
        )

        await service._maybe_auto_pause_after_close(session=session, config_id=config.id)
        await session.commit()

        await session.refresh(config)
        assert config.is_running is False
        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config.id)
            )
        ).all()
        types = [event.event_type for event in events]
        assert "kpi_guard_triggered" in types
        assert "strategy_auto_paused" in types
        triggered = next(e for e in events if e.event_type == "kpi_guard_triggered")
        assert "daily_loss" in [b["rule"] for b in triggered.payload["breaches"]]
        first_count = len(events)

        # Idempotent: the strategy is now paused ⇒ the hook returns early, no events.
        await service._maybe_auto_pause_after_close(session=session, config_id=config.id)
        events_after = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config.id)
            )
        ).all()
        assert len(events_after) == first_count


async def test_on_close_hook_fails_open_on_unavailable_balance(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1.4 — pct daily-loss with an unreachable balance: degraded warning, NEVER pause."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    # Simulate an unavailable balance (the exchange call fails → fail-open None).
    monkeypatch.setattr(
        service, "_safe_subaccount_usdt_balance", AsyncMock(return_value=None)
    )
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config.id,
                enabled=True,
                kpi_guard_enabled=True,
                kpi_guard_max_daily_loss_pct=50.0,
                conflicting_signal_policy="off",
            )
        )
        await _seed_closed_positions(
            session,
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            close_prices=[80.0, 80.0, 80.0],  # a real loss today
            base=datetime.now(UTC),
        )

        await service._maybe_auto_pause_after_close(session=session, config_id=config.id)
        await session.commit()

        await session.refresh(config)
        assert config.is_running is True  # fail-open: not paused on a missing balance
        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config.id)
            )
        ).all()
        types = [event.event_type for event in events]
        assert "risk_check_degraded" in types
        assert "strategy_auto_paused" not in types


async def test_kill_switch_close_position_closes_emits_and_is_idempotent(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2.3 — the kill-switch close reuses _flatten_single_position, emits
    kill_switch_triggered, and is idempotent on an already-closed position."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        position = _make_open_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            idx=0,
        )
        session.add(position)
        await session.commit()

        async def fake_flatten(*, session: Any, config: Any, position_row: Any, reason: str) -> None:
            assert reason == "volatility_kill_switch"
            position_row.status = "closed"
            position_row.state = "closed"

        monkeypatch.setattr(service, "_flatten_single_position", fake_flatten)

        signal = KillSwitchSignal(
            should_close=True, reason="atr_spike", actual=250.0, threshold=200.0
        )
        ok = await service.kill_switch_close_position(
            session=session, position_id=position.id, signal=signal, commit=True
        )
        assert ok is True

        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.position_id == position.id)
            )
        ).all()
        evt = next(e for e in events if e.event_type == "kill_switch_triggered")
        assert evt.payload["reason"] == "atr_spike"
        assert evt.payload["closed"] is True
        assert evt.payload["actual"] == 250.0

        # Idempotent: the position is now closed ⇒ a second call no-ops (no re-flatten).
        calls = {"n": 0}

        async def counting_flatten(**_kw: Any) -> None:
            calls["n"] += 1

        monkeypatch.setattr(service, "_flatten_single_position", counting_flatten)
        ok2 = await service.kill_switch_close_position(
            session=session, position_id=position.id, signal=signal, commit=True
        )
        assert ok2 is False
        assert calls["n"] == 0


async def test_kill_switch_close_position_records_failure_without_closing(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2.3 — a failed flatten records kill_switch_triggered (closed=False, error)
    and leaves the position open; it is not retried into a loop here."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        position = _make_open_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            idx=0,
        )
        session.add(position)
        await session.commit()

        async def failing_flatten(*, session: Any, config: Any, position_row: Any, reason: str) -> None:
            return None  # leaves status "open" — the close did not complete

        monkeypatch.setattr(service, "_flatten_single_position", failing_flatten)

        signal = KillSwitchSignal(
            should_close=True, reason="price_move", actual=8.0, threshold=5.0
        )
        ok = await service.kill_switch_close_position(
            session=session, position_id=position.id, signal=signal, commit=True
        )
        assert ok is False

        await session.refresh(position)
        assert position.status == "open"
        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.position_id == position.id)
            )
        ).all()
        evt = next(e for e in events if e.event_type == "kill_switch_triggered")
        assert evt.payload["closed"] is False
        assert evt.level == "error"


async def test_kill_switch_latches_risk_off_on_trip(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2.4 — a kill-switch trip latches the RUNNING strategy risk-off (paused),
    emitting risk_off_entered + strategy_auto_paused on top of kill_switch_triggered."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        position = _make_open_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            idx=0,
        )
        session.add(position)
        await session.commit()

        async def fake_flatten(*, session: Any, config: Any, position_row: Any, reason: str) -> None:
            position_row.status = "closed"
            position_row.state = "closed"

        monkeypatch.setattr(service, "_flatten_single_position", fake_flatten)

        signal = KillSwitchSignal(
            should_close=True, reason="atr_spike", actual=300.0, threshold=200.0
        )
        ok = await service.kill_switch_close_position(
            session=session, position_id=position.id, signal=signal, commit=True
        )
        assert ok is True

        await session.refresh(config)
        assert config.is_running is False  # risk-off latched

        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config.id)
            )
        ).all()
        types = {event.event_type for event in events}
        assert "kill_switch_triggered" in types
        assert "risk_off_entered" in types
        assert "strategy_auto_paused" in types


async def test_kill_switch_latches_risk_off_even_when_close_fails(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2.4 — even a FAILED close latches risk-off: a volatility spike must stop
    new entries (the open position remains; we must not open more)."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        position = _make_open_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            idx=0,
        )
        session.add(position)
        await session.commit()

        async def failing_flatten(*, session: Any, config: Any, position_row: Any, reason: str) -> None:
            return None  # close did not complete — position stays open

        monkeypatch.setattr(service, "_flatten_single_position", failing_flatten)

        signal = KillSwitchSignal(
            should_close=True, reason="price_move", actual=9.0, threshold=5.0
        )
        ok = await service.kill_switch_close_position(
            session=session, position_id=position.id, signal=signal, commit=True
        )
        assert ok is False  # close failed

        await session.refresh(config)
        assert config.is_running is False  # still latched risk-off
        await session.refresh(position)
        assert position.status == "open"  # the position remains open
        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config.id)
            )
        ).all()
        types = {event.event_type for event in events}
        assert "risk_off_entered" in types
        assert "strategy_auto_paused" in types


async def test_apply_kill_switch_config_populates_context_from_risk_row(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T2.3b — _apply_kill_switch_config copies the strategy's kill-switch config
    onto the PositionContext so the realtime tick can evaluate it."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        position = _make_open_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            idx=0,
        )
        session.add(position)
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config.id,
                enabled=True,
                kill_switch_enabled=True,
                kill_switch_atr_spike_mult=3.0,
                kill_switch_atr_period=14,
                kill_switch_price_move_pct=5.0,
                kill_switch_cooldown_seconds=30,
                conflicting_signal_policy="off",
            )
        )
        await session.commit()

        ctx = PositionContext(
            position_id=str(position.id), symbol="BTC/USDT:USDT", account_id=str(account_id)
        )
        await service._apply_kill_switch_config(session=session, position=ctx)

        assert ctx.kill_switch_enabled is True
        assert ctx.kill_switch_atr_spike_mult == 3.0
        assert ctx.kill_switch_atr_period == 14
        assert ctx.kill_switch_price_move_pct == 5.0
        assert ctx.kill_switch_cooldown_seconds == 30


async def test_apply_kill_switch_config_is_noop_without_risk_row(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """T2.3b — no risk row (or kill-switch off) ⇒ the context stays off (fail-safe)."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        position = _make_open_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            idx=0,
        )
        session.add(position)
        await session.commit()

        ctx = PositionContext(
            position_id=str(position.id), symbol="BTC/USDT:USDT", account_id=str(account_id)
        )
        await service._apply_kill_switch_config(session=session, position=ctx)

        assert ctx.kill_switch_enabled is False
        assert ctx.kill_switch_atr_spike_mult is None


async def test_apply_kpi_guard_commits_degraded_warning_when_commit_true(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I1 — apply_kpi_guard(commit=True) must be self-contained: a
    degraded-only outcome (pct rule skipped on an unavailable balance, no pause)
    persists its risk_check_degraded event rather than relying on the caller to
    commit. Verified via a SECOND session (uncommitted writes roll back on close)."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config.id,
                enabled=True,
                kpi_guard_enabled=True,
                kpi_guard_max_daily_loss_pct=50.0,
                conflicting_signal_policy="off",
            )
        )
        await session.commit()
        config_id = config.id

        # A loss today + unavailable balance ⇒ pct rule degraded (skipped), no pause.
        monkeypatch.setattr(service, "_today_realized_pnl_usdt", AsyncMock(return_value=-50.0))
        monkeypatch.setattr(service, "_safe_subaccount_usdt_balance", AsyncMock(return_value=None))
        healthy = StrategyHealth(
            config_id=config_id,
            window_days=30,
            sample_size=12,
            win_rate_pct=70.0,
            max_dd_pct=5.0,
            total_pnl_usdt=10.0,
            roi_pct=10.0,
            sharpe_proxy=1.0,
            stability_score=0.7,
            health_score=80.0,
            health_class="healthy",
            computed_at=datetime.now(UTC),
        )
        decision = await service.apply_kpi_guard(
            session=session, config_id=config_id, health=healthy, commit=True
        )
        assert decision.should_pause is False
        assert decision.warning is not None
        # NB: this session is NOT committed by the test — it rolls back on close.

    # Fresh session / connection: the degraded event must have been COMMITTED.
    async with auto_trade_db() as verify:
        events = (
            await verify.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.config_id == config_id)
            )
        ).all()
        assert "risk_check_degraded" in [event.event_type for event in events]


def _snapshot_health(config_id: int, *, computed_at: datetime, max_dd_pct: float) -> StrategyHealth:
    return StrategyHealth(
        config_id=config_id,
        window_days=30,
        sample_size=12,
        win_rate_pct=50.0,
        max_dd_pct=max_dd_pct,
        total_pnl_usdt=0.0,
        roi_pct=0.0,
        sharpe_proxy=0.0,
        stability_score=0.5,
        health_score=50.0,
        health_class="warning",
        computed_at=computed_at,
    )


async def test_latest_health_snapshots_for_configs_returns_latest_per_config(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Review S2 — batch lookup returns the most-recent snapshot per config in one query."""
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        await session.commit()
        await record_health_snapshot(
            session=session,
            health=_snapshot_health(
                config.id, computed_at=datetime(2026, 6, 1, tzinfo=UTC), max_dd_pct=10.0
            ),
            user_id=user.id,
        )
        await record_health_snapshot(
            session=session,
            health=_snapshot_health(
                config.id, computed_at=datetime(2026, 6, 2, tzinfo=UTC), max_dd_pct=30.0
            ),
            user_id=user.id,
        )
        await session.commit()

        latest = await latest_health_snapshots_for_configs(
            session=session, config_ids=[config.id]
        )
        assert set(latest.keys()) == {config.id}
        assert latest[config.id].max_dd_pct == 30.0  # the newer snapshot wins
        # Empty input ⇒ empty dict (no query).
        assert await latest_health_snapshots_for_configs(session=session, config_ids=[]) == {}


async def test_prune_strategy_health_snapshots_drops_old_keeps_recent(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Review S1 — retention prune drops snapshots older than the cutoff, keeps recent."""
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        await session.commit()
        await record_health_snapshot(
            session=session,
            health=_snapshot_health(
                config.id, computed_at=datetime(2026, 1, 1, tzinfo=UTC), max_dd_pct=10.0
            ),
            user_id=user.id,
        )
        await record_health_snapshot(
            session=session,
            health=_snapshot_health(config.id, computed_at=datetime.now(UTC), max_dd_pct=20.0),
            user_id=user.id,
        )
        await session.commit()

        cutoff = datetime.now(UTC) - timedelta(days=90)
        deleted = await prune_strategy_health_snapshots(
            session=session, config_id=config.id, cutoff=cutoff
        )
        await session.commit()

        remaining = (
            await session.scalars(
                select(StrategyHealthSnapshot).where(
                    StrategyHealthSnapshot.config_id == config.id
                )
            )
        ).all()
        assert deleted == 1
        assert len(remaining) == 1
        assert remaining[0].max_dd_pct == 20.0  # recent kept


async def test_kill_switch_end_to_end_on_tick_closes_position(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review S6 — full chain: a spike tick into RealtimeSLAdjuster.on_tick fires
    the kill_switch_handler, which closes the position via the service and emits
    kill_switch_triggered. (Locks the on_tick → handler → close → DB wiring.)"""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        config.is_running = True
        position = _make_open_position(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            idx=0,
        )
        session.add(position)
        await session.commit()
        pid = position.id

        async def fake_flatten(
            *, session: Any, config: Any, position_row: Any, reason: str
        ) -> None:
            assert reason == "volatility_kill_switch"
            position_row.status = "closed"
            position_row.state = "closed"

        monkeypatch.setattr(service, "_flatten_single_position", fake_flatten)

        async def handler(pos: PositionContext, signal: KillSwitchSignal) -> None:
            await service.kill_switch_close_position(
                session=session, position_id=int(pos.position_id), signal=signal, commit=True
            )

        adjuster = RealtimeSLAdjuster(
            symbol="BTC/USDT:USDT",
            queue_resolver=AsyncMock(),
            client_order_id_factory=lambda _pid, _kind: "coid",
            persist_handler=AsyncMock(),
            kill_switch_handler=handler,
        )
        ctx = PositionContext(
            position_id=str(pid),
            symbol="BTC/USDT:USDT",
            state=PositionState.OPEN,
            side=PositionSide.LONG,
        )
        ctx.kill_switch_enabled = True
        ctx.kill_switch_price_move_pct = 5.0  # price-move trigger (no ATR buffer needed)

        # A -8% bar (100000 → 92000): |move| 8 ≥ 5 ⇒ kill-switch trips.
        await adjuster.on_tick(
            {
                "open_time": 1,
                "open": 100_000.0,
                "high": 100_000.0,
                "low": 92_000.0,
                "close": 92_000.0,
                "is_closed": True,
            },
            [ctx],
        )

        await session.refresh(position)
        assert position.status == "closed"
        await session.refresh(config)
        assert config.is_running is False  # risk-off latched
        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.position_id == pid)
            )
        ).all()
        assert "kill_switch_triggered" in [event.event_type for event in events]


def test_risk_config_schema_accepts_off_policy() -> None:
    # I5 — 'off' is a valid policy (conflict blocking is opt-in) and the default.
    assert AutoTradeRiskConfig().conflicting_signal_policy == "off"
    assert AutoTradeRiskConfig(conflicting_signal_policy="off").conflicting_signal_policy == "off"
    # W9 T1.1 — the KPI-Guard is opt-in: absent ⇒ disabled, every threshold off.
    default = AutoTradeRiskConfig()
    assert default.kpi_guard_enabled is False
    assert default.kpi_guard_max_dd_pct is None
    assert default.kpi_guard_max_daily_loss_usdt is None
    assert default.kpi_guard_max_daily_loss_pct is None
    assert default.kpi_guard_min_win_rate_pct is None
    assert default.kpi_guard_min_trades is None
    # W9 T2.1 — the Volatility Kill-Switch is opt-in too: absent ⇒ disabled, params off.
    assert default.kill_switch_enabled is False
    assert default.kill_switch_atr_spike_mult is None
    assert default.kill_switch_atr_period is None
    assert default.kill_switch_price_move_pct is None
    assert default.kill_switch_cooldown_seconds is None


def test_risk_config_schema_rejects_kpi_guard_out_of_bounds() -> None:
    # W9 T1.1 — API-edge bounds mirror the DB CHECKs (Pydantic 422 before the DB).
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(kpi_guard_max_dd_pct=150.0)  # > 100
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(kpi_guard_max_dd_pct=0.0)  # not > 0
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(kpi_guard_min_trades=0)  # not >= 1
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(kpi_guard_min_win_rate_pct=120.0)  # > 100


def test_risk_config_schema_rejects_kill_switch_out_of_bounds() -> None:
    # W9 T2.1 — API-edge bounds mirror the DB CHECKs.
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(kill_switch_atr_spike_mult=1.0)  # not > 1
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(kill_switch_atr_period=1)  # not >= 2
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(kill_switch_price_move_pct=0.0)  # not > 0
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(kill_switch_cooldown_seconds=-1)  # not >= 0


async def test_pre_trade_risk_conflicting_signal_off_allows_opposite(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """I5 — with policy 'off' an opposite open position does NOT block (opt-in only)."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        session.add(
            _make_open_position(
                user_id=user.id,
                config_id=config.id,
                profile_id=profile.id,
                account_id=account_id,
                symbol="BTC/USDT:USDT",
                idx=0,
                side="SHORT",
            )
        )
        await session.commit()

        decision = await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=AutoTradeRiskConfigModel(enabled=True, conflicting_signal_policy="off"),
            signal=cast(Any, SimpleNamespace(trend="LONG")),
            execution_symbol="BTC/USDT:USDT",
        )
        assert decision.allowed is True


async def test_safe_balance_fails_open_and_logs_on_exchange_error(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I3 — an expected exchange/IO error fails OPEN (None) but is logged, not silent."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    monkeypatch.setattr(
        service._trading,
        "get_spot_balances",
        AsyncMock(side_effect=TimeoutError("exchange timeout")),
        raising=False,
    )
    with caplog.at_level(logging.WARNING):
        result = await service._safe_subaccount_usdt_balance(
            session=cast(Any, None), user_id=1, account_id=1
        )
    assert result is None
    assert "balance fetch failed" in caplog.text


async def test_safe_balance_propagates_unexpected_error(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I3 — a code bug (unexpected exception) must NOT be swallowed (fail-closed, visible)."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    monkeypatch.setattr(
        service._trading,
        "get_spot_balances",
        AsyncMock(side_effect=AttributeError("refactor bug")),
        raising=False,
    )
    with pytest.raises(AttributeError):
        await service._safe_subaccount_usdt_balance(
            session=cast(Any, None), user_id=1, account_id=1
        )


async def test_risk_config_db_rejects_out_of_range_upper_bounds(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """I4 — the DB CHECKs reject limits above the API bounds (defense-in-depth)."""

    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        await session.commit()
        config_id = config.id

    # Each violating insert rolls back, so a fresh session reuses the same config.
    async with auto_trade_db() as session:
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config_id,
                enabled=True,
                leverage_ceiling=10_000,  # > 125
                conflicting_signal_policy="block_opposite",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with auto_trade_db() as session:
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config_id,
                enabled=True,
                daily_loss_limit_pct=5_000.0,  # > 100
                conflicting_signal_policy="block_opposite",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    # W9 T1.1 — the KPI-Guard thresholds carry the same DB-level backstop.
    async with auto_trade_db() as session:
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config_id,
                enabled=True,
                kpi_guard_enabled=True,
                kpi_guard_max_dd_pct=150.0,  # > 100
                conflicting_signal_policy="off",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    # W9 T2.1 — the Kill-Switch thresholds carry the same DB-level backstop.
    async with auto_trade_db() as session:
        session.add(
            AutoTradeRiskConfigModel(
                config_id=config_id,
                enabled=True,
                kill_switch_enabled=True,
                kill_switch_atr_spike_mult=0.5,  # not > 1
                conflicting_signal_policy="off",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


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


@pytest.mark.parametrize(
    "core_confidence,should_open",
    [
        # Threshold in the config is 62.0 (percent). The AI core may emit
        # confidence as either a percent (65) or a fraction (0.65) — both
        # forms must be treated identically as 65 % and admit the trade.
        (0.65, True),    # fraction above threshold → opens
        (65, True),      # integer percent above → opens
        (65.0, True),    # float percent above → opens
        # And the inverse: a fraction below the threshold must block.
        # Before the fix this was the user-reported defect: 0.56 in the
        # signal vs 65 in the (correctly-typed) config let trades through
        # because the gate compared mixed units; now 0.56 is normalized
        # to 56 % and the comparison is unit-consistent.
        (0.56, False),   # fraction below → blocked
        (56, False),     # integer percent below → blocked
        # Boundary: the gate uses strict ``<`` (``if signal < config:
        # return``), so a signal equal to the threshold is admitted.
        # Both 0.62 (normalized to 62) and 62 must therefore open a trade.
        (0.62, True),
        (62, True),
    ],
)
async def test_auto_trade_gate_treats_fraction_and_percent_identically(
    auto_trade_db: async_sessionmaker[AsyncSession],
    core_confidence: float | int,
    should_open: bool,
) -> None:
    """Regression: the entry gate must produce the same open/block decision
    regardless of whether the AI core emits confidence as a fraction
    (``0.65``) or a percent (``65``).

    Before the symmetry fix, fractional values from a strict-contract
    source bypassed the normalization and the gate compared
    ``0.65 < 62`` (always True) instead of ``65 < 62`` (False), keeping
    every fraction-source signal blocked. Conversely, the legacy
    ``analysisStructured`` path normalized correctly, so the user-visible
    behaviour depended on which signal producer was active.
    """
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
            trade_job_id=f"job-conf-{core_confidence}-{should_open}",
            signal_payload=_build_signal(
                trend="LONG",
                confidence_pct=core_confidence,
            ),
        )
        await service.enqueue_history_signal(session=session, history=history)
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        open_position = await service.get_open_position(
            session=session,
            user_id=user.id,
        )
        if should_open:
            assert open_position is not None, (
                f"Expected to open trade for core_confidence={core_confidence}, "
                f"but no position was opened."
            )
            assert fake_trading.order_calls, "expected at least one order call"
        else:
            assert open_position is None, (
                f"Expected gate to block core_confidence={core_confidence}, "
                f"but a position was opened: {open_position!r}"
            )
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


def _ledger_fill(
    *,
    user_id: int,
    account_id: int,
    position_id: int,
    exchange_trade_id: str,
    side: str,
    price: float,
    amount: float,
    fee_cost: float,
    realized_pnl: float | None,
    traded_at: datetime,
) -> ExchangeTradeLedger:
    return ExchangeTradeLedger(
        user_id=user_id,
        account_id=account_id,
        exchange_name="binance",
        market_type="futures",
        symbol="BTC/USDT:USDT",
        exchange_trade_id=exchange_trade_id,
        exchange_order_id=None,
        client_order_id=None,
        side=side,
        price=price,
        amount=amount,
        cost=price * amount,
        fee_cost=fee_cost,
        fee_currency="USDT",
        realized_pnl=realized_pnl,
        traded_at=traded_at,
        ingested_at=traded_at,
        origin="platform",
        origin_confidence="strong",
        auto_trade_position_id=position_id,
        raw_trade={},
    )


def _funding_row(
    *, user_id: int, account_id: int, tran_id: str, income: float, income_at: datetime
) -> ExchangeIncomeLedger:
    return ExchangeIncomeLedger(
        user_id=user_id,
        account_id=account_id,
        exchange_name="binance",
        market_type="futures",
        income_type="FUNDING_FEE",
        asset="USDT",
        income=income,
        symbol="BTC/USDT:USDT",
        tran_id=tran_id,
        trade_id=None,
        info="FUNDING_FEE",
        income_at=income_at,
        ingested_at=income_at,
        raw={},
    )


async def test_open_snapshot_realized_includes_partial_closes_and_funding(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """OPEN position with a multi-TP partial close: realized must reflect the
    closed part's realized_pnl − commission + funding, not just −fees."""
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=200.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        opened_at = datetime.now(UTC) - timedelta(hours=10)
        position = AutoTradePosition(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            side="LONG",
            status="open",
            entry_price=100.0,
            quantity=1.0,
            position_size_usdt=200.0,
            leverage=1,
            tp_price=110.0,
            sl_price=95.0,
            entry_confidence_pct=70.0,
            opened_at=opened_at,
            raw_open_order={},
            raw_close_order={},
        )
        session.add(position)
        await session.flush()
        # Live position keeps the remaining 1.0 open (unrealized 0 from the fake).
        fake_trading.set_external_position(
            symbol="BTC/USDT:USDT", side="long", contracts=1.0, account_id=account_id
        )
        session.add_all(
            [
                _ledger_fill(
                    user_id=user.id, account_id=account_id, position_id=position.id,
                    exchange_trade_id="open-1", side="buy", price=100.0, amount=2.0,
                    fee_cost=0.08, realized_pnl=0.0, traded_at=opened_at,
                ),
                _ledger_fill(
                    user_id=user.id, account_id=account_id, position_id=position.id,
                    exchange_trade_id="tp-1", side="sell", price=106.0, amount=1.0,
                    fee_cost=0.05, realized_pnl=12.0, traded_at=opened_at + timedelta(hours=1),
                ),
                _funding_row(
                    user_id=user.id, account_id=account_id, tran_id="fund-1",
                    income=-0.5, income_at=opened_at + timedelta(hours=2),
                ),
            ]
        )
        await session.commit()
        await session.refresh(position)

        snapshot = await service.build_position_pnl_snapshot(
            session=session, user_id=user.id, position=position
        )
        # gross 12.0 − commission 0.13 + funding (−0.5) = 11.37 (not −0.13).
        assert snapshot["realized_pnl_usdt"] == pytest.approx(11.37)
        assert snapshot["total_pnl_usdt"] == pytest.approx(11.37)
        assert snapshot["gross_realized_usdt"] == pytest.approx(12.0)
        assert snapshot["commission_usdt"] == pytest.approx(0.13)
        assert snapshot["funding_usdt"] == pytest.approx(-0.5)
        assert snapshot["net_pnl_usdt"] == pytest.approx(11.37)


async def test_closed_snapshot_uses_ledger_realized_and_funding_multi_tp(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """CLOSED multi-TP: realized = Σ realized_pnl − commission + funding, not a
    single close_price × quantity approximation."""
    fake_trading = _FakeTradingService()
    service = AutoTradeService(trading_service=cast(Any, fake_trading))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=200.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        opened_at = datetime.now(UTC) - timedelta(hours=10)
        closed_at = opened_at + timedelta(hours=3)
        position = AutoTradePosition(
            user_id=user.id,
            config_id=config.id,
            profile_id=profile.id,
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            side="LONG",
            status="closed",
            entry_price=100.0,
            quantity=2.0,
            position_size_usdt=200.0,
            leverage=1,
            tp_price=110.0,
            sl_price=95.0,
            entry_confidence_pct=70.0,
            opened_at=opened_at,
            closed_at=closed_at,
            close_reason="tp",
            close_price=95.0,  # misleading single price — realized must come from the ledger
            raw_open_order={},
            raw_close_order={},
        )
        session.add(position)
        await session.flush()
        session.add_all(
            [
                _ledger_fill(
                    user_id=user.id, account_id=account_id, position_id=position.id,
                    exchange_trade_id="open-1", side="buy", price=100.0, amount=2.0,
                    fee_cost=0.08, realized_pnl=0.0, traded_at=opened_at,
                ),
                _ledger_fill(
                    user_id=user.id, account_id=account_id, position_id=position.id,
                    exchange_trade_id="tp-1", side="sell", price=104.0, amount=1.0,
                    fee_cost=0.05, realized_pnl=8.0, traded_at=opened_at + timedelta(hours=1),
                ),
                _ledger_fill(
                    user_id=user.id, account_id=account_id, position_id=position.id,
                    exchange_trade_id="tp-2", side="sell", price=102.0, amount=1.0,
                    fee_cost=0.05, realized_pnl=4.0, traded_at=opened_at + timedelta(hours=2),
                ),
                _funding_row(
                    user_id=user.id, account_id=account_id, tran_id="fund-1",
                    income=-0.5, income_at=opened_at + timedelta(hours=1, minutes=30),
                ),
            ]
        )
        await session.commit()
        await session.refresh(position)

        snapshot = await service.build_position_pnl_snapshot(
            session=session, user_id=user.id, position=position
        )
        # gross 12.0 − commission 0.18 + funding (−0.5) = 11.32 (not directional −10).
        assert snapshot["realized_pnl_usdt"] == pytest.approx(11.32)
        assert snapshot["total_pnl_usdt"] == pytest.approx(11.32)
        assert snapshot["gross_realized_usdt"] == pytest.approx(12.0)
        assert snapshot["commission_usdt"] == pytest.approx(0.18)
        assert snapshot["funding_usdt"] == pytest.approx(-0.5)
        assert snapshot["net_pnl_usdt"] == pytest.approx(11.32)


async def test_today_realized_pnl_is_net_from_ledger_pure_db(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Daily-loss numerator = net (Σ realized_pnl − commission + funding) from the
    local ledger/income for today's fills — and computed with zero exchange
    calls, so the gate stays off the trading hot path."""

    class _ExplodingTrading:
        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            async def _boom(*_: object, **__: object) -> object:
                raise AssertionError(f"unexpected exchange call '{name}' on daily-loss path")

            return _boom

    service = AutoTradeService(trading_service=cast(Any, _ExplodingTrading()))

    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=AutoTradeConfigUpsertRequest(
                enabled=True,
                profile_id=profile.id,
                account_id=account_id,
                position_size_usdt=200.0,
                leverage=1,
                min_confidence_pct=62.0,
                fast_close_confidence_pct=80.0,
                confirm_reports_required=2,
                risk_mode="1:2",
                sl_pct=1.0,
                tp_pct=2.0,
            ),
        )
        today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)

        def _fill(trade_id: str, side: str, fee: float, realized: float) -> ExchangeTradeLedger:
            return ExchangeTradeLedger(
                user_id=user.id,
                account_id=account_id,
                exchange_name="binance",
                market_type="futures",
                symbol="BTC/USDT:USDT",
                exchange_trade_id=trade_id,
                side=side,
                price=100.0,
                amount=2.0,
                cost=200.0,
                fee_cost=fee,
                fee_currency="USDT",
                realized_pnl=realized,
                traded_at=today,
                ingested_at=today,
                origin="platform",
                origin_confidence="strong",
                auto_trade_config_id=config.id,
            )

        session.add_all(
            [
                _fill("open-1", "buy", 0.08, 0.0),
                _fill("close-1", "sell", 0.05, -20.0),  # a losing close
                _funding_row(
                    user_id=user.id, account_id=account_id, tran_id="f1",
                    income=-0.5, income_at=today,
                ),
            ]
        )
        await session.commit()

        # net = gross(−20) − commission(0.13) + funding(−0.5) = −20.63
        value = await service._today_realized_pnl_usdt(session=session, config_id=config.id)
        assert value == pytest.approx(-20.63)


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


# ---------------------------------------------------------------------------
# W4 / Phase 1: AI Trend Overlay — entry-side lock
# ---------------------------------------------------------------------------


def _signal_with_ai_trend(
    *,
    trend: str,
    confidence_pct: float,
    ai_trend_direction: str | None,
    ai_trend_strength: float = 0.9,
    symbol: str = "BTCUSDT",
    decision_event_id: str | None = None,
    reasoning_path: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload = _build_signal(trend=trend, confidence_pct=confidence_pct, symbol=symbol)
    if ai_trend_direction is not None:
        payload["aiTrend"] = {
            "direction": ai_trend_direction,
            "strength": ai_trend_strength,
            "probabilitiesPct": {"up": 50, "down": 30, "flat": 20},
        }
    if decision_event_id is not None:
        payload["decisionEventId"] = decision_event_id
    if reasoning_path is not None:
        payload["reasoningPath"] = reasoning_path
    return payload


async def _enable_overlay(
    session: AsyncSession,
    *,
    user_id: int,
    entry_side_lock_enabled: bool = True,
    atr_scaling_enabled: bool = False,
    rsi_scaling_enabled: bool = False,
) -> None:
    row = cast(
        AutoTradeConfig,
        await session.scalar(select(AutoTradeConfig).where(AutoTradeConfig.user_id == user_id)),
    )
    row.ai_overlay_config_json = {
        "enabled": True,
        "entry_side_lock_enabled": entry_side_lock_enabled,
        "atr_scaling_enabled": atr_scaling_enabled,
        "rsi_scaling_enabled": rsi_scaling_enabled,
        "stale_max_minutes": 240,
        "min_strength": 0.4,
        "atr_scale_range": [0.8, 1.2],
        "rsi_max_shift": 5,
    }
    await session.commit()


async def test_ai_overlay_allows_entry_when_side_aligned_with_trend(
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
        await _enable_overlay(session, user_id=user.id)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-overlay-align",
            signal_payload=_signal_with_ai_trend(
                trend="LONG", confidence_pct=70.0, ai_trend_direction="up"
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is not None
        assert open_position.side == "LONG"
        # No block event recorded
        blocks = (
            await session.scalars(
                select(AutoTradeEvent).where(
                    AutoTradeEvent.user_id == user.id,
                    AutoTradeEvent.event_type == "ai_overlay_block_entry",
                )
            )
        ).all()
        assert blocks == []


async def test_ai_overlay_blocks_entry_when_side_opposes_trend(
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
        await _enable_overlay(session, user_id=user.id)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-overlay-block",
            signal_payload=_signal_with_ai_trend(
                trend="SHORT", confidence_pct=70.0, ai_trend_direction="up"
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        # No position opened
        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is None
        # No exchange order placed
        assert fake_trading.order_calls == []
        # Block event recorded with correct payload
        block_rows = list(
            (
                await session.scalars(
                    select(AutoTradeEvent).where(
                        AutoTradeEvent.user_id == user.id,
                        AutoTradeEvent.event_type == "ai_overlay_block_entry",
                    )
                )
            ).all()
        )
        assert len(block_rows) == 1
        payload = block_rows[0].payload
        assert payload["reason"] == "ai_trend_up_blocks_short"
        assert payload["ai_trend"]["direction"] == "up"
        assert payload["intended_side"] == "short"


async def test_ai_overlay_fail_open_when_ai_trend_missing(
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
        await _enable_overlay(session, user_id=user.id)

        # Signal payload has no aiTrend → resolver returns None → fail-open.
        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-overlay-stale",
            signal_payload=_signal_with_ai_trend(
                trend="SHORT", confidence_pct=70.0, ai_trend_direction=None
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        # Position IS opened (fail-open preserves pre-overlay behaviour).
        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is not None
        assert open_position.side == "SHORT"
        # Stale fallback event recorded
        stale_rows = list(
            (
                await session.scalars(
                    select(AutoTradeEvent).where(
                        AutoTradeEvent.user_id == user.id,
                        AutoTradeEvent.event_type == "ai_overlay_stale_fallback",
                    )
                )
            ).all()
        )
        assert len(stale_rows) == 1
        assert stale_rows[0].payload["phase"] == "entry_side_lock"


async def test_ai_overlay_atr_scaled_event_emitted_on_aligned_long(
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
        # Pin strategy profile so we know the base ATR multiplier (2.5).
        row = cast(
            AutoTradeConfig,
            await session.scalar(select(AutoTradeConfig).where(AutoTradeConfig.user_id == user.id)),
        )
        row.strategy_profile_json = _strategy_profile_payload()
        await session.commit()
        await _enable_overlay(
            session,
            user_id=user.id,
            entry_side_lock_enabled=False,
            atr_scaling_enabled=True,
        )

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-overlay-atr",
            signal_payload=_signal_with_ai_trend(
                trend="LONG",
                confidence_pct=70.0,
                ai_trend_direction="up",
                ai_trend_strength=1.0,
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        atr_rows = list(
            (
                await session.scalars(
                    select(AutoTradeEvent).where(
                        AutoTradeEvent.user_id == user.id,
                        AutoTradeEvent.event_type == "ai_overlay_atr_scaled",
                    )
                )
            ).all()
        )
        assert len(atr_rows) == 1
        payload = atr_rows[0].payload
        # Base = 2.5 (from _strategy_profile_payload), strength=1.0 → factor=1.2 → 3.0.
        assert payload["before"] == pytest.approx(2.5)
        assert payload["after"] == pytest.approx(3.0)
        assert payload["ai_trend"]["direction"] == "up"
        assert payload["position_side"] == "long"
        assert payload["reason"] == "trend_aligned_widen"


async def test_ai_overlay_atr_no_event_on_flat_trend(
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
        await _enable_overlay(
            session,
            user_id=user.id,
            entry_side_lock_enabled=False,
            atr_scaling_enabled=True,
        )

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-overlay-atr-flat",
            signal_payload=_signal_with_ai_trend(
                trend="LONG",
                confidence_pct=70.0,
                ai_trend_direction="flat",
                ai_trend_strength=1.0,
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        atr_count = await session.scalar(
            select(func.count(AutoTradeEvent.id)).where(
                AutoTradeEvent.user_id == user.id,
                AutoTradeEvent.event_type == "ai_overlay_atr_scaled",
            )
        )
        assert atr_count == 0


async def test_ai_overlay_rsi_shifts_watcher_condition_thresholds(
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
        # Strategy profile has an RSI watcher with condition "> 75".
        row = cast(
            AutoTradeConfig,
            await session.scalar(select(AutoTradeConfig).where(AutoTradeConfig.user_id == user.id)),
        )
        row.strategy_profile_json = _strategy_profile_payload()
        await session.commit()
        await _enable_overlay(
            session,
            user_id=user.id,
            entry_side_lock_enabled=False,
            atr_scaling_enabled=False,
            rsi_scaling_enabled=True,
        )

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-overlay-rsi",
            signal_payload=_signal_with_ai_trend(
                trend="LONG",
                confidence_pct=70.0,
                ai_trend_direction="up",
                ai_trend_strength=1.0,
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        # Audit event recorded
        rsi_rows = list(
            (
                await session.scalars(
                    select(AutoTradeEvent).where(
                        AutoTradeEvent.user_id == user.id,
                        AutoTradeEvent.event_type == "ai_overlay_rsi_scaled",
                    )
                )
            ).all()
        )
        assert len(rsi_rows) == 1
        payload = rsi_rows[0].payload
        # strength=1.0 with default max_shift=5 → +5.
        assert payload["before"] == [30, 70]
        assert payload["after"] == [35, 75]
        assert payload["shift"] == 5

        # Persisted position has watchers with shifted condition.
        position = cast(
            AutoTradePosition,
            await session.scalar(
                select(AutoTradePosition).where(AutoTradePosition.user_id == user.id)
            ),
        )
        watchers = list(position.active_watchers_json)
        rsi_watchers = [w for w in watchers if str(w.get("indicator", "")).upper() == "RSI"]
        assert len(rsi_watchers) == 1
        # Base condition was "> 75", shifted by +5 → "> 80".
        assert rsi_watchers[0]["condition"] == "> 80"


async def test_ai_overlay_disabled_by_default_does_not_interfere(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Without overlay opt-in, behaviour must be byte-identical to pre-W4."""
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
        # Note: NOT calling _enable_overlay.

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-overlay-off",
            signal_payload=_signal_with_ai_trend(
                # Would have been blocked if overlay were on.
                trend="SHORT", confidence_pct=70.0, ai_trend_direction="up"
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        open_position = await service.get_open_position(session=session, user_id=user.id)
        assert open_position is not None
        # No overlay events should exist at all.
        overlay_event_count = await session.scalar(
            select(func.count(AutoTradeEvent.id)).where(
                AutoTradeEvent.user_id == user.id,
                AutoTradeEvent.event_type.like("ai_overlay_%"),
            )
        )
        assert overlay_event_count == 0


# ---------------------------------------------------------------------------
# W2: traceability — decision_event_id + reasoning_path in audit payloads
# ---------------------------------------------------------------------------


async def test_ai_overlay_block_entry_payload_includes_decision_event_id_and_reasoning(
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
        await _enable_overlay(session, user_id=user.id)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-w2-block",
            signal_payload=_signal_with_ai_trend(
                trend="SHORT",
                confidence_pct=70.0,
                ai_trend_direction="up",
                decision_event_id="evt-w2-trace-001",
                reasoning_path=[
                    {
                        "agentKey": "twitterSentiment",
                        "signal": "up",
                        "confidence": 0.82,
                        "weight": 0.30,
                        "summary": "Heavy bullish chatter.",
                    },
                    {
                        "agentKey": "techModelSignal",
                        "signal": "up",
                        "confidence": 0.78,
                        "weight": 0.25,
                    },
                    {
                        "agentKey": "researchFundamental",
                        "signal": "flat",
                        "confidence": 0.40,
                        "weight": 0.10,
                    },
                ],
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        block_event = cast(
            AutoTradeEvent,
            await session.scalar(
                select(AutoTradeEvent).where(
                    AutoTradeEvent.user_id == user.id,
                    AutoTradeEvent.event_type == "ai_overlay_block_entry",
                )
            ),
        )
        assert block_event is not None
        payload = block_event.payload

        # decisionEventId is denormalised into ai_trend block
        assert payload["ai_trend"]["decision_event_id"] == "evt-w2-trace-001"

        # reasoning_path is denormalised at the top level
        assert "reasoning_path" in payload
        reasoning = payload["reasoning_path"]
        assert isinstance(reasoning, list) and len(reasoning) == 3
        # Sorted by weight DESC, so twitterSentiment (0.30) comes first
        assert reasoning[0]["agent_key"] == "twitterSentiment"
        assert reasoning[0]["weight"] == 0.30
        assert reasoning[0]["signal"] == "up"
        # Summary survives the round-trip
        assert reasoning[0]["summary"] == "Heavy bullish chatter."
        # Order preserved by weight: techModelSignal (0.25) → researchFundamental (0.10)
        assert [r["agent_key"] for r in reasoning] == [
            "twitterSentiment",
            "techModelSignal",
            "researchFundamental",
        ]


async def test_ai_overlay_persists_decision_event_id_on_open_position(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """When overlay is on and snapshot carries decisionEventId, position links to it."""
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
        # Overlay enabled but ATR/RSI off — we only need the snapshot to be
        # resolved (entry_side_lock_enabled keeps the resolve path active).
        await _enable_overlay(session, user_id=user.id)

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-w2-link",
            signal_payload=_signal_with_ai_trend(
                # Aligned: ai_trend up + LONG signal → entry allowed.
                trend="LONG",
                confidence_pct=70.0,
                ai_trend_direction="up",
                decision_event_id="evt-w2-position-link-001",
                reasoning_path=[
                    {"agentKey": "twitterSentiment", "signal": "up", "weight": 0.3}
                ],
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        stats = await service.process_signal_queue(session=session)
        assert stats["completed"] == 1

        position = cast(
            AutoTradePosition,
            await session.scalar(
                select(AutoTradePosition).where(AutoTradePosition.user_id == user.id)
            ),
        )
        assert position is not None
        assert position.decision_event_id == "evt-w2-position-link-001"


async def test_position_decision_event_id_null_when_overlay_disabled(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Without overlay, positions get NULL decision_event_id (regression guard)."""
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
        # NOT enabling overlay.

        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-w2-no-overlay",
            signal_payload=_signal_with_ai_trend(
                trend="LONG",
                confidence_pct=70.0,
                ai_trend_direction="up",
                decision_event_id="evt-should-not-be-persisted",
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        await service.process_signal_queue(session=session)

        position = cast(
            AutoTradePosition,
            await session.scalar(
                select(AutoTradePosition).where(AutoTradePosition.user_id == user.id)
            ),
        )
        assert position is not None
        assert position.decision_event_id is None


async def test_ai_overlay_block_entry_works_without_optional_traceability(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """Legacy records without decisionEventId/reasoningPath must still block."""
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
        await _enable_overlay(session, user_id=user.id)

        # No decision_event_id, no reasoning_path supplied.
        history = await _create_history(
            session,
            user_id=user.id,
            profile_id=profile.id,
            trade_job_id="job-w2-legacy",
            signal_payload=_signal_with_ai_trend(
                trend="SHORT", confidence_pct=70.0, ai_trend_direction="up"
            ),
        )
        assert await service.enqueue_history_signal(session=session, history=history) is True
        await service.process_signal_queue(session=session)

        block_event = cast(
            AutoTradeEvent,
            await session.scalar(
                select(AutoTradeEvent).where(
                    AutoTradeEvent.user_id == user.id,
                    AutoTradeEvent.event_type == "ai_overlay_block_entry",
                )
            ),
        )
        assert block_event is not None
        payload = block_event.payload
        # Optional fields gracefully absent
        assert "decision_event_id" not in payload["ai_trend"]
        assert "reasoning_path" not in payload
        # But the block still happened
        assert payload["reason"] == "ai_trend_up_blocks_short"


# ---------------------------------------------------------------------------
# Phase A — Supervisor completeness (audit follow-up §2)
# ---------------------------------------------------------------------------


async def test_leverage_ceiling_above_exchange_max_rejected(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """A1: Bybit max leverage is 100. A ceiling of 110 passes the schema bound
    (<=125) but is unattainable on the venue, so the upsert must reject it."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)  # bybit
        with pytest.raises(ValueError, match="leverage"):
            await service.upsert_config(
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
                    risk=AutoTradeRiskConfig(leverage_ceiling=110),
                ),
            )
        # And no half-created config should remain.
        remaining = (await session.scalars(select(AutoTradeConfig))).all()
        assert remaining == []


async def test_leverage_ceiling_within_exchange_max_accepted(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """A1: a ceiling at the venue max (Bybit 100) is accepted and persisted."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)  # bybit
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
                risk=AutoTradeRiskConfig(leverage_ceiling=100),
            ),
        )
        risk = await service.get_risk_config(session=session, config_id=config.id)
        assert risk is not None
        assert risk.leverage_ceiling == 100


async def _upsert_basic_config(
    service: "AutoTradeService",
    session: AsyncSession,
    *,
    user_id: int,
    profile_id: int,
    account_id: int,
) -> AutoTradeConfig:
    return await service.upsert_config(
        session=session,
        user_id=user_id,
        payload=AutoTradeConfigUpsertRequest(
            enabled=True,
            profile_id=profile_id,
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


async def test_bulk_apply_risk_config_updates_all_user_strategies(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """A2: one call writes identical risk limits to every config of the user."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        profile2, account_id2 = await _create_profile_and_account(
            session, user_id=user.id, symbol="ETHUSDT", account_label="second"
        )
        cfg1 = await _upsert_basic_config(
            service, session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        cfg2 = await _upsert_basic_config(
            service, session, user_id=user.id, profile_id=profile2.id, account_id=account_id2
        )

        count = await service.apply_risk_config_to_all_strategies(
            session=session,
            user_id=user.id,
            risk=AutoTradeRiskConfig(daily_loss_limit_usdt=50.0, max_open_positions=2),
        )

        assert count == 2
        r1 = await service.get_risk_config(session=session, config_id=cfg1.id)
        r2 = await service.get_risk_config(session=session, config_id=cfg2.id)
        assert r1 is not None and r1.daily_loss_limit_usdt == 50.0 and r1.max_open_positions == 2
        assert r2 is not None and r2.daily_loss_limit_usdt == 50.0 and r2.max_open_positions == 2


async def test_bulk_apply_risk_config_leaves_other_users_untouched(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """A2: apply-all is scoped to the caller — another user's config is untouched."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user_a, profile_a, account_a = await _seed_user_profile_and_account(session)
        cfg_a = await _upsert_basic_config(
            service, session, user_id=user_a.id, profile_id=profile_a.id, account_id=account_a
        )
        user_b = User(email="b@example.com", hashed_password="x", is_active=True)
        session.add(user_b)
        await session.flush()
        profile_b, account_b = await _create_profile_and_account(
            session, user_id=user_b.id, symbol="BTCUSDT", account_label="b-main"
        )
        cfg_b = await _upsert_basic_config(
            service, session, user_id=user_b.id, profile_id=profile_b.id, account_id=account_b
        )

        count = await service.apply_risk_config_to_all_strategies(
            session=session,
            user_id=user_a.id,
            risk=AutoTradeRiskConfig(daily_loss_limit_usdt=10.0),
        )

        assert count == 1
        assert (await service.get_risk_config(session=session, config_id=cfg_a.id)) is not None
        # B's config got no risk row.
        assert (await service.get_risk_config(session=session, config_id=cfg_b.id)) is None


async def test_bulk_apply_rejects_leverage_above_venue_and_applies_nothing(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """A2: a leverage ceiling above a config's venue max aborts the whole batch
    (atomic) — no config ends up with a partial risk row."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)  # bybit max 100
        cfg1 = await _upsert_basic_config(
            service, session, user_id=user.id, profile_id=profile.id, account_id=account_id
        )
        cfg1_id = cfg1.id  # capture before the aborted transaction expires the ORM object
        with pytest.raises(ValueError, match="leverage"):
            await service.apply_risk_config_to_all_strategies(
                session=session,
                user_id=user.id,
                risk=AutoTradeRiskConfig(leverage_ceiling=110),
            )
        await session.rollback()
        assert (await service.get_risk_config(session=session, config_id=cfg1_id)) is None


async def _trip_kill_switch(
    service: "AutoTradeService",
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_id: int,
    profile_id: int,
    account_id: int,
    reason: str = "atr_spike",
) -> AutoTradeConfig:
    """Helper: seed a running config + open position and trip the kill-switch."""
    config = await _insert_config(
        session, user_id=user_id, profile_id=profile_id, account_id=account_id
    )
    config.is_running = True
    position = _make_open_position(
        user_id=user_id,
        config_id=config.id,
        profile_id=profile_id,
        account_id=account_id,
        symbol="BTC/USDT:USDT",
        idx=0,
    )
    session.add(position)
    await session.commit()

    async def fake_flatten(*, session: Any, config: Any, position_row: Any, reason: str) -> None:
        position_row.status = "closed"
        position_row.state = "closed"

    monkeypatch.setattr(service, "_flatten_single_position", fake_flatten)
    signal = KillSwitchSignal(should_close=True, reason=reason, actual=300.0, threshold=200.0)
    await service.kill_switch_close_position(
        session=session, position_id=position.id, signal=signal, commit=True
    )
    return config


async def test_kill_switch_risk_off_latch_persists_across_restart(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3: the risk-off latch set by a kill-switch trip is persisted, so it
    survives a process restart (read back from a fresh session)."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _trip_kill_switch(
            service, session, monkeypatch,
            user_id=user.id, profile_id=profile.id, account_id=account_id, reason="atr_spike",
        )
        config_id = config.id

    # Fresh session — simulates a process restart reading the persisted latch.
    async with auto_trade_db() as session2:
        reloaded = await session2.get(AutoTradeConfig, config_id)
        assert reloaded is not None
        assert reloaded.risk_off_latched is True
        assert reloaded.risk_off_reason == "atr_spike"
        assert reloaded.risk_off_at is not None


async def test_resume_clears_risk_off_latch(
    auto_trade_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3: a manual resume (set_running True) clears the risk-off latch."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _trip_kill_switch(
            service, session, monkeypatch,
            user_id=user.id, profile_id=profile.id, account_id=account_id,
        )
        resumed = await service.set_running(
            session=session, user_id=user.id, is_running=True, account_id=account_id
        )
        assert resumed.is_running is True
        assert resumed.risk_off_latched is False
        assert resumed.risk_off_reason is None
        assert resumed.risk_off_at is None


# ---------------------------------------------------------------------------
# Phase B — Manual spot path through the supervisor (audit follow-up §3)
# ---------------------------------------------------------------------------


async def test_precheck_manual_order_blocks_on_risk_violation(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """B1: a manual OPEN order on an account with a risk config is gated by the
    pre-trade engine — a leverage-ceiling violation blocks it + audits it."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id, leverage=10
        )
        await service._apply_risk_config(
            session=session, config_id=config.id, risk=AutoTradeRiskConfig(leverage_ceiling=5)
        )
        decision = await service.precheck_manual_order(
            session=session,
            user_id=user.id,
            account_id=account_id,
            symbol="BTCUSDT",
            side="LONG",
            price=100.0,
        )
        assert decision.allowed is False
        assert decision.rule == "leverage"
        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.event_type == "risk_blocked")
            )
        ).all()
        assert len(events) == 1


async def test_precheck_manual_order_allows_within_limits(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """B1: a manual order within the account's risk limits is allowed."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id, leverage=2
        )
        await service._apply_risk_config(
            session=session, config_id=config.id, risk=AutoTradeRiskConfig(leverage_ceiling=5)
        )
        decision = await service.precheck_manual_order(
            session=session,
            user_id=user.id,
            account_id=account_id,
            symbol="BTCUSDT",
            side="LONG",
            price=100.0,
        )
        assert decision.allowed is True


async def test_precheck_manual_order_no_config_is_allowed(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """B1 fail-safe: an account with no auto-trade config keeps today's behaviour
    (manual order allowed, no risk envelope to apply)."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, _profile, account_id = await _seed_user_profile_and_account(session)
        decision = await service.precheck_manual_order(
            session=session,
            user_id=user.id,
            account_id=account_id,
            symbol="BTCUSDT",
            side="LONG",
            price=100.0,
        )
        assert decision.allowed is True


async def test_precheck_manual_order_no_risk_config_is_allowed(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """B1 fail-safe: a config without a risk row is a no-op (allow)."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id, leverage=10
        )
        decision = await service.precheck_manual_order(
            session=session,
            user_id=user.id,
            account_id=account_id,
            symbol="BTCUSDT",
            side="LONG",
            price=100.0,
        )
        assert decision.allowed is True


async def test_precheck_manual_order_sell_is_never_gated(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """B1: a reducing order (spot sell / SHORT) is de-risking and is never blocked,
    even when an opening order would violate the same risk limit."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await _insert_config(
            session, user_id=user.id, profile_id=profile.id, account_id=account_id, leverage=10
        )
        await service._apply_risk_config(
            session=session, config_id=config.id, risk=AutoTradeRiskConfig(leverage_ceiling=5)
        )
        decision = await service.precheck_manual_order(
            session=session,
            user_id=user.id,
            account_id=account_id,
            symbol="BTCUSDT",
            side="SHORT",
            price=100.0,
        )
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Phase D — Append-only config revisions + content hash + rollback (§7)
# ---------------------------------------------------------------------------


async def _revisions_for(
    session: AsyncSession, config_id: int
) -> list[AutoTradeConfigRevision]:
    return list(
        (
            await session.scalars(
                select(AutoTradeConfigRevision)
                .where(AutoTradeConfigRevision.config_id == config_id)
                .order_by(AutoTradeConfigRevision.revision_number)
            )
        ).all()
    )


def _revision_upsert_payload(
    *, profile_id: int, account_id: int, position_size_usdt: float = 100.0
) -> AutoTradeConfigUpsertRequest:
    return AutoTradeConfigUpsertRequest(
        enabled=True,
        profile_id=profile_id,
        account_id=account_id,
        position_size_usdt=position_size_usdt,
        leverage=3,
        min_confidence_pct=62.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
    )


async def test_upsert_config_records_initial_revision(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """D2: creating a config records revision #1 with a content hash + snapshot."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(profile_id=profile.id, account_id=account_id),
        )
        revisions = await _revisions_for(session, config.id)
        assert len(revisions) == 1
        assert revisions[0].revision_number == 1
        assert len(revisions[0].content_hash) == 64
        assert revisions[0].snapshot_json["position_size_usdt"] == 100.0


async def test_editing_config_records_new_revision(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """D2: a content change appends an immutable revision #2."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(profile_id=profile.id, account_id=account_id),
        )
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(
                profile_id=profile.id, account_id=account_id, position_size_usdt=250.0
            ),
        )
        revisions = await _revisions_for(session, config.id)
        assert [r.revision_number for r in revisions] == [1, 2]
        assert revisions[0].snapshot_json["position_size_usdt"] == 100.0
        assert revisions[1].snapshot_json["position_size_usdt"] == 250.0
        assert revisions[0].content_hash != revisions[1].content_hash


async def test_identical_resave_does_not_duplicate_revision(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """D2: re-saving identical content does not append a revision (hash dedup)."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(profile_id=profile.id, account_id=account_id),
        )
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(profile_id=profile.id, account_id=account_id),
        )
        revisions = await _revisions_for(session, config.id)
        assert len(revisions) == 1


async def test_rollback_config_restores_prior_revision(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """D3: rollback restores the prior content AND appends a new revision."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(profile_id=profile.id, account_id=account_id),
        )
        rev1 = (await _revisions_for(session, config.id))[0]
        rev1_id = rev1.id
        await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(
                profile_id=profile.id, account_id=account_id, position_size_usdt=250.0
            ),
        )
        rolled = await service.rollback_config(
            session=session, user_id=user.id, config_id=config.id, revision_id=rev1_id
        )
        assert rolled.position_size_usdt == 100.0
        revisions = await _revisions_for(session, config.id)
        assert [r.revision_number for r in revisions] == [1, 2, 3]
        assert revisions[2].snapshot_json["position_size_usdt"] == 100.0
        assert revisions[2].content_hash == revisions[0].content_hash


async def test_rollback_config_rejects_other_users_config(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """D3: a user cannot roll back another user's config (ownership)."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user_a, profile_a, account_a = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user_a.id,
            payload=_revision_upsert_payload(profile_id=profile_a.id, account_id=account_a),
        )
        rev_id = (await _revisions_for(session, config.id))[0].id
        other = User(email="other@example.com", hashed_password="x", is_active=True)
        session.add(other)
        await session.commit()
        with pytest.raises(LookupError):
            await service.rollback_config(
                session=session, user_id=other.id, config_id=config.id, revision_id=rev_id
            )


async def test_rollback_config_rejects_revision_of_other_config(
    auto_trade_db: async_sessionmaker[AsyncSession],
) -> None:
    """D3: a revision_id that does not belong to the target config is rejected."""
    service = AutoTradeService(trading_service=cast(Any, _FakeTradingService()))
    async with auto_trade_db() as session:
        user, profile, account_id = await _seed_user_profile_and_account(session)
        config = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(profile_id=profile.id, account_id=account_id),
        )
        config_id = config.id
        profile2, account_id2 = await _create_profile_and_account(
            session, user_id=user.id, symbol="ETHUSDT", account_label="second"
        )
        config2 = await service.upsert_config(
            session=session,
            user_id=user.id,
            payload=_revision_upsert_payload(profile_id=profile2.id, account_id=account_id2),
        )
        foreign_rev_id = (await _revisions_for(session, config2.id))[0].id
        with pytest.raises(LookupError):
            await service.rollback_config(
                session=session, user_id=user.id, config_id=config_id, revision_id=foreign_rev_id
            )
