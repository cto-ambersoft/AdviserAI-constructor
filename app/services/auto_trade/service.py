import asyncio
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar, cast

from pydantic import ValidationError
from sqlalchemy import Select, desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_event import AutoTradeEvent
from app.models.auto_trade_position import AutoTradePosition
from app.models.auto_trade_signal_queue import AutoTradeSignalQueue
from app.models.auto_trade_signal_state import AutoTradeSignalState
from app.models.exchange import ExchangeCredential
from app.models.exchange_order_metadata import ExchangeOrderMetadata
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.schemas.auto_trade import AutoTradeConfigUpsertRequest
from app.schemas.exchange_trading import NormalizedFuturesPosition, NormalizedTrade, OrderSide
from app.schemas.strategy_profile import StrategyProfileConfig
from app.services.exchange.adapter import (
    ConditionalOrderResult,
    EntryOrderResult,
    OrderSide as ExchangeOrderSide,
)
from app.services.exchange_credentials.service import ExchangeCredentialsService
from app.services.execution.errors import ExchangeServiceError
from app.services.execution.trading_service import TradingService
from app.services.personal_analysis.provider import AnalysisProviderError
from app.services.position.context import (
    PositionContext,
    PositionSide as RuntimePositionSide,
    TPLevel,
    WatcherConfig,
)
from app.services.position.order_queue import OrderPriority, OrderTask
from app.services.position.state_machine import PositionState, TransitionTrigger
from app.services.sl_tp.multi_tp import MultiTPEngine
from app.services.watchers.service import create_exchange_adapter_for_position, get_order_queue
from app.services.ws.manager import WebSocketManager

from .signal import (
    ParsedAutoTradeSignal,
    adapt_legacy_analysis_structured_payload,
    parse_auto_trade_signal,
    symbol_market_key,
    to_chart_symbol,
    to_linear_perp_symbol,
)

QUEUE_PENDING = "pending"
QUEUE_PROCESSING = "processing"
QUEUE_COMPLETED = "completed"
QUEUE_DEAD = "dead"
_QUEUE_ACTIVE_STATUSES = (QUEUE_PENDING,)

POSITION_OPEN = "open"
POSITION_CLOSED = "closed"

EVENT_LEVEL_INFO = "info"
EVENT_LEVEL_WARNING = "warning"
EVENT_LEVEL_ERROR = "error"

TREND_LONG = "LONG"
TREND_SHORT = "SHORT"
TREND_NEUTRAL = "NEUTRAL"
_POSITION_CLOSE_CONFIRM_ATTEMPTS = 3
_POSITION_CLOSE_CONFIRM_DELAY_SECONDS = 0.35
_POSITION_EPSILON = 1e-9
_OPEN_POSITION_STATE_NAMES = {
    PositionState.PENDING.value,
    PositionState.ENTERING.value,
    PositionState.OPEN.value,
    PositionState.ADJUSTING.value,
    PositionState.CLOSING.value,
    PositionState.RECONNECTING.value,
    PositionState.ERROR_RECOVERY.value,
}
_WS_MANAGER_REGISTRY: dict[str, WebSocketManager] = {}
_WS_MANAGER_LOCK: asyncio.Lock | None = None

T = TypeVar("T")
_SUPPORTED_AUTO_TRADE_FUTURES_EXCHANGES = {"bybit", "binance"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _serialize_strategy_profile(
    strategy_profile: Any,
) -> dict[str, object] | None:
    if strategy_profile is None:
        return None
    payload = strategy_profile.model_dump(mode="json")
    return cast(dict[str, object], payload)


def _get_ws_manager_lock() -> asyncio.Lock:
    global _WS_MANAGER_LOCK
    if _WS_MANAGER_LOCK is None:
        _WS_MANAGER_LOCK = asyncio.Lock()
    return _WS_MANAGER_LOCK


def _parse_strategy_profile(
    payload: dict[str, object] | None,
) -> StrategyProfileConfig | None:
    if payload is None:
        return None
    try:
        return StrategyProfileConfig.model_validate(payload)
    except ValidationError:
        return None


class AutoTradeService:
    def __init__(
        self,
        trading_service: TradingService | None = None,
        *,
        use_exchange_adapter_entry: bool | None = None,
    ) -> None:
        self._trading = trading_service or TradingService()
        self._use_exchange_adapter_entry = (
            isinstance(self._trading, TradingService)
            if use_exchange_adapter_entry is None
            else bool(use_exchange_adapter_entry)
        )
        self._credentials_service = ExchangeCredentialsService()
        settings = get_settings()
        self._status_batch_size = settings.auto_trade_status_batch_size
        self._max_attempts = settings.auto_trade_max_attempts
        self._retry_interval_seconds = settings.auto_trade_retry_interval_seconds
        self._scheduler_loop_enabled = settings.auto_trade_scheduler_loop_enabled

    @staticmethod
    def _runtime_position_side(trend: str) -> RuntimePositionSide:
        if trend == TREND_SHORT:
            return RuntimePositionSide.SHORT
        return RuntimePositionSide.LONG

    @staticmethod
    def _closing_exchange_order_side(side: RuntimePositionSide) -> ExchangeOrderSide:
        if side == RuntimePositionSide.SHORT:
            return ExchangeOrderSide.BUY
        return ExchangeOrderSide.SELL

    @staticmethod
    def _status_from_runtime_state(state: PositionState) -> str:
        if state == PositionState.CLOSED:
            return POSITION_CLOSED
        if state in {PositionState.CANCELLED, PositionState.FAILED}:
            return "error"
        return POSITION_OPEN

    @staticmethod
    def _to_decimal_or_none(value: float | None) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(float(value)))

    @staticmethod
    def _parse_optional_datetime(value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    async def _resolve_account_exchange_name(
        self,
        *,
        session: AsyncSession,
        account_id: int,
    ) -> str:
        exchange_name = await session.scalar(
            select(ExchangeCredential.exchange_name).where(ExchangeCredential.id == account_id)
        )
        return str(exchange_name or "")

    def _build_position_context(
        self,
        *,
        config: AutoTradeConfig,
        signal: ParsedAutoTradeSignal,
        exchange_name: str,
        execution_symbol: str,
        quantity: float,
        tp_price: float,
        sl_price: float,
    ) -> PositionContext:
        strategy_profile = _parse_strategy_profile(config.strategy_profile_json)
        side = self._runtime_position_side(signal.trend)

        tp_mode = "single"
        tp_levels: list[TPLevel] = []
        current_tp_price = tp_price
        trailing_enabled = False
        trailing_callback_rate: float | None = None
        breakeven_enabled = False
        breakeven_trigger_rr = 1.0
        volatility_enabled = False
        volatility_atr_period = 14
        volatility_atr_multiplier = 2.0
        active_watchers: list[WatcherConfig] = []
        adjustment_priority = ["watcher", "trailing", "breakeven", "volatility"]
        sl_type = "fixed"

        if strategy_profile is not None:
            sl_type = strategy_profile.sl_mode
            tp_mode = strategy_profile.tp_mode
            trailing_enabled = strategy_profile.trailing_enabled
            trailing_callback_rate = (
                float(strategy_profile.trailing_callback_rate)
                if strategy_profile.trailing_enabled
                else None
            )
            breakeven_enabled = strategy_profile.breakeven_enabled
            breakeven_trigger_rr = float(strategy_profile.breakeven_trigger_rr)
            volatility_enabled = strategy_profile.volatility_sl_enabled
            volatility_atr_period = int(strategy_profile.volatility_atr_period)
            volatility_atr_multiplier = float(strategy_profile.volatility_atr_multiplier)
            adjustment_priority = list(strategy_profile.adjustment_priority)
            active_watchers = [
                WatcherConfig(
                    indicator=watcher.indicator,
                    params=dict(watcher.params),
                    condition=watcher.condition,
                    action=watcher.action,
                    action_params=dict(watcher.action_params),
                    is_active=watcher.is_active,
                )
                for watcher in strategy_profile.watchers
            ]
            if strategy_profile.tp_mode == "multi" and strategy_profile.tp_levels:
                tp_levels = []
                for index, level in enumerate(strategy_profile.tp_levels):
                    tp_level = TPLevel.from_offset(
                        level=index + 1,
                        price_offset_pct=float(level.price_offset_pct),
                        close_pct=float(level.close_pct),
                        entry_price=signal.price_current,
                        side=side,
                        move_sl_to=level.move_sl_to,
                        sl_lock_pct=(
                            float(level.sl_lock_pct)
                            if level.sl_lock_pct is not None
                            else None
                        ),
                    )
                    tp_levels.append(tp_level)
                if tp_levels:
                    current_tp_price = tp_levels[0].trigger_price

        return PositionContext(
            user_id=str(config.user_id),
            account_id=str(config.account_id),
            exchange=exchange_name,
            symbol=execution_symbol,
            state=PositionState.PENDING,
            side=side,
            entry_price=float(signal.price_current),
            original_quantity=float(quantity),
            current_quantity=float(quantity),
            leverage=int(config.leverage),
            current_sl_price=float(sl_price),
            sl_type=sl_type,
            tp_mode=tp_mode,
            tp_levels=tp_levels,
            current_tp_price=float(current_tp_price) if current_tp_price is not None else None,
            trailing_enabled=trailing_enabled,
            trailing_callback_rate=trailing_callback_rate,
            breakeven_enabled=breakeven_enabled,
            breakeven_trigger_rr=breakeven_trigger_rr,
            volatility_sl_enabled=volatility_enabled,
            volatility_atr_period=volatility_atr_period,
            volatility_atr_multiplier=volatility_atr_multiplier,
            active_watchers=active_watchers,
            adjustment_priority=adjustment_priority,
        )

    def _refresh_runtime_prices_after_fill(
        self,
        *,
        position: PositionContext,
        config: AutoTradeConfig,
        trend: str,
        entry_price: float,
        filled_quantity: float,
    ) -> None:
        tp_price, sl_price = self._calculate_tp_sl_for_entry_price(
            entry_price=entry_price,
            trend=trend,
            config=config,
        )
        position.entry_price = float(entry_price)
        position.original_quantity = float(filled_quantity)
        position.current_quantity = float(filled_quantity)
        position.current_sl_price = float(sl_price)
        position.opened_at = _utc_now().isoformat()
        position.last_adjusted_at = position.opened_at

        if position.tp_mode == "multi" and position.tp_levels:
            refreshed_levels: list[TPLevel] = []
            for level in position.tp_levels:
                refreshed = TPLevel.from_offset(
                    level=level.level,
                    price_offset_pct=level.price_offset_pct,
                    close_pct=level.close_pct,
                    entry_price=entry_price,
                    side=position.side,
                    status=level.status,
                    exchange_order_id=level.exchange_order_id,
                    move_sl_to=level.move_sl_to,
                    sl_lock_pct=level.sl_lock_pct,
                )
                refreshed_levels.append(refreshed)
            position.tp_levels = refreshed_levels
            position.current_tp_price = (
                position.tp_levels[0].trigger_price if position.tp_levels else float(tp_price)
            )
            return

        position.current_tp_price = float(tp_price)

    def _merge_position_context_into_row(
        self,
        *,
        row: AutoTradePosition,
        position: PositionContext,
    ) -> None:
        payload = position.to_db_dict()
        row.state = position.state.value
        row.status = self._status_from_runtime_state(position.state)
        row.original_quantity = self._to_decimal_or_none(position.original_quantity)
        row.current_quantity = self._to_decimal_or_none(position.current_quantity)
        if position.state != PositionState.CLOSED and position.current_quantity > _POSITION_EPSILON:
            row.quantity = float(position.current_quantity)
        elif row.quantity <= _POSITION_EPSILON and position.original_quantity > _POSITION_EPSILON:
            row.quantity = float(position.original_quantity)

        if position.current_sl_price > _POSITION_EPSILON:
            row.sl_price = float(position.current_sl_price)
        current_tp_price = self._resolve_runtime_tp_price(position)
        if current_tp_price is not None and current_tp_price > _POSITION_EPSILON:
            row.tp_price = float(current_tp_price)

        row.sl_exchange_order_id = position.sl_exchange_order_id
        row.sl_type = position.sl_type
        row.sl_history_json = payload["sl_history_json"]
        row.tp_mode = position.tp_mode
        row.tp_levels_json = payload["tp_levels_json"]
        row.tp_history_json = payload["tp_history_json"]
        row.trailing_config_json = payload["trailing_config_json"]
        row.breakeven_config_json = payload["breakeven_config_json"]
        row.volatility_config_json = payload["volatility_config_json"]
        row.active_watchers_json = payload["active_watchers_json"]
        row.adjustment_priority_json = payload["adjustment_priority_json"]
        row.transition_log_json = payload["transition_log_json"]

        opened_at = self._parse_optional_datetime(position.opened_at)
        if opened_at is not None:
            row.opened_at = opened_at

        closed_at = self._parse_optional_datetime(position.closed_at)
        if closed_at is not None:
            row.closed_at = closed_at
        elif position.state == PositionState.CLOSED and row.closed_at is None:
            row.closed_at = _utc_now()

        last_adjusted_at = self._parse_optional_datetime(position.last_adjusted_at)
        row.last_adjusted_at = last_adjusted_at

    @staticmethod
    def _resolve_runtime_tp_price(position: PositionContext) -> float | None:
        if position.tp_mode == "multi" and position.tp_levels:
            return float(position.tp_levels[0].trigger_price)
        if position.current_tp_price is None:
            return None
        return float(position.current_tp_price)

    def _entry_result_from_legacy_order(self, *, payload: Any) -> EntryOrderResult:
        order = payload.order
        return EntryOrderResult(
            exchange_order_id=order.id,
            client_order_id=str(order.client_order_id or ""),
            symbol=order.symbol,
            side=ExchangeOrderSide(order.side),
            order_type=order.order_type,
            status=order.status,
            quantity=float(order.amount),
            filled_quantity=float(order.filled),
            remaining_quantity=float(order.remaining),
            price=float(order.price) if order.price is not None else None,
            average_price=float(order.average) if order.average is not None else None,
            cost=float(order.cost) if order.cost is not None else None,
            timestamp=order.timestamp,
            raw=order.raw if isinstance(order.raw, dict) else {},
        )

    async def _create_exchange_adapter(
        self,
        *,
        session: AsyncSession,
        position: PositionContext,
    ) -> Any:
        return await create_exchange_adapter_for_position(position, session=session)

    async def _place_entry_order(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        position: PositionContext,
        side: ExchangeOrderSide,
        quantity: float,
        client_order_id: str,
        take_profit_price: float | None,
        stop_loss_price: float | None,
    ) -> tuple[EntryOrderResult, Any | None]:
        # Multi-TP delegates TP placement to MultiTPEngine; bracket only attaches SL.
        bracket_tp = take_profit_price if position.tp_mode == "single" else None
        bracket_sl = stop_loss_price

        sl_coid = (
            f"{client_order_id}-sl" if bracket_sl is not None else None
        )
        tp_coid = (
            f"{client_order_id}-tp" if bracket_tp is not None else None
        )

        if self._use_exchange_adapter_entry:
            adapter = await self._create_exchange_adapter(session=session, position=position)
            result = await adapter.place_entry_order(
                symbol=position.symbol,
                side=side,
                quantity=quantity,
                client_order_id=client_order_id,
                take_profit_price=bracket_tp,
                stop_loss_price=bracket_sl,
                sl_client_order_id=sl_coid,
                tp_client_order_id=tp_coid,
            )
            return result, adapter

        opened = await self._trading.place_futures_market_order(
            session=session,
            user_id=config.user_id,
            account_id=config.account_id,
            symbol=position.symbol,
            side=cast(OrderSide, side.value),
            amount=quantity,
            reduce_only=False,
            client_order_id=client_order_id,
            take_profit_price=bracket_tp,
            stop_loss_price=bracket_sl,
        )
        return self._entry_result_from_legacy_order(payload=opened), None

    async def _persist_runtime_position(self, position: PositionContext) -> None:
        try:
            position_id = int(position.position_id)
        except (TypeError, ValueError):
            return

        async with AsyncSessionFactory() as session:
            row = await session.get(AutoTradePosition, position_id)
            if row is None:
                return
            self._merge_position_context_into_row(row=row, position=position)
            await session.commit()

    def _build_conditional_order_callback(
        self,
        *,
        position: PositionContext,
        source: str,
        level_index: int | None = None,
    ):
        async def _callback(result: Any) -> None:
            if not isinstance(result, ConditionalOrderResult):
                return

            if source == "sl":
                position.sl_exchange_order_id = result.exchange_order_id
            elif source == "tp" and level_index is not None and 0 <= level_index < len(position.tp_levels):
                level = position.tp_levels[level_index]
                level.exchange_order_id = result.exchange_order_id
                level.status = "open"

            await self._persist_runtime_position(position)

        return _callback

    async def _schedule_position_watchers(self, position: PositionContext) -> str | None:
        from app.services.watchers.scheduling import schedule_position_watcher

        return await schedule_position_watcher(position)

    async def _ensure_ws_manager_tracked(
        self,
        *,
        session: AsyncSession,
        position: PositionContext,
    ) -> None:
        lock = _get_ws_manager_lock()
        async with lock:
            manager = _WS_MANAGER_REGISTRY.get(position.account_id)
            if manager is None:
                adapter = await self._create_exchange_adapter(session=session, position=position)
                manager = WebSocketManager(
                    adapter=adapter,
                    account_id=position.account_id,
                    persist_position=self._persist_runtime_position,
                    order_queue_resolver=lambda current: get_order_queue(current),
                )
                _WS_MANAGER_REGISTRY[position.account_id] = manager
                await manager.start()

            manager.track_position(position)

    async def _initialize_position_runtime(
        self,
        *,
        session: AsyncSession,
        position: PositionContext,
        adapter: Any | None,
        opened: EntryOrderResult,
    ) -> None:
        if adapter is None:
            return

        queue = await get_order_queue(position, session=session)

        bracket_sl = opened.attached_sl
        bracket_tp = opened.attached_tp

        if bracket_sl is not None:
            position.sl_exchange_order_id = bracket_sl.exchange_order_id
        else:
            await queue.enqueue(
                OrderTask(
                    priority=OrderPriority.NEW_CONDITIONAL,
                    created_at=datetime.now(UTC).timestamp(),
                    position_id=position.position_id,
                    action="place_sl",
                    params={
                        "symbol": position.symbol,
                        "side": self._closing_exchange_order_side(position.side),
                        "quantity": float(position.current_quantity),
                        "full_quantity": float(position.current_quantity),
                        "trigger_price": float(position.current_sl_price),
                        "client_order_id": self._build_position_runtime_order_id(
                            position_id=position.position_id,
                            kind="sl",
                        ),
                        "reduce_only": True,
                    },
                    on_success=self._build_conditional_order_callback(
                        position=position,
                        source="sl",
                    ),
                )
            )

        if position.tp_mode == "multi" and position.tp_levels:
            engine = MultiTPEngine(
                position=position,
                adapter=adapter,
                order_queue=queue,
                task_callback_factory=lambda level_index, _level: self._build_conditional_order_callback(
                    position=position,
                    source="tp",
                    level_index=level_index,
                ),
            )
            await engine.initialize_tp_levels()
        elif bracket_tp is not None:
            if position.tp_levels:
                position.tp_levels[0].exchange_order_id = bracket_tp.exchange_order_id
                position.tp_levels[0].status = "open"
        elif position.current_tp_price is not None and position.current_tp_price > _POSITION_EPSILON:
            await queue.enqueue(
                OrderTask(
                    priority=OrderPriority.NEW_CONDITIONAL,
                    created_at=datetime.now(UTC).timestamp(),
                    position_id=position.position_id,
                    action="place_tp",
                    params={
                        "symbol": position.symbol,
                        "side": self._closing_exchange_order_side(position.side),
                        "quantity": float(position.current_quantity),
                        "trigger_price": float(position.current_tp_price),
                        "client_order_id": self._build_position_runtime_order_id(
                            position_id=position.position_id,
                            kind="tp",
                        ),
                        "reduce_only": True,
                    },
                    on_success=self._build_conditional_order_callback(
                        position=position,
                        source="tp",
                    ),
                )
            )

        await self._persist_runtime_position(position)

        if position.active_watchers:
            await self._schedule_position_watchers(position)

        await self._ensure_ws_manager_tracked(session=session, position=position)

    async def _emergency_close_unprotected_position(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        position: AutoTradePosition,
        position_context: PositionContext,
        adapter: Any | None,
        history: PersonalAnalysisHistory,
        reason: str,
        filled_quantity: float,
    ) -> None:
        """Flatten an entry that was opened without confirmed protective orders."""
        closing_side = self._closing_exchange_order_side(position_context.side)
        rollback_client_order_id = self._build_position_runtime_order_id(
            position_id=position_context.position_id, kind="emergency",
        )

        close_error: str | None = None
        try:
            if adapter is not None:
                await adapter.partial_close(
                    symbol=position_context.symbol,
                    side=closing_side,
                    quantity=float(filled_quantity),
                    client_order_id=rollback_client_order_id,
                    order_type="market",
                )
            else:
                await self._trading.place_futures_market_order(
                    session=session,
                    user_id=config.user_id,
                    account_id=config.account_id,
                    symbol=position_context.symbol,
                    side=closing_side.value,
                    amount=float(filled_quantity),
                    reduce_only=True,
                    client_order_id=rollback_client_order_id,
                    take_profit_price=None,
                    stop_loss_price=None,
                )
        except Exception as exc:
            close_error = str(exc)

        position.status = POSITION_CLOSED
        position.state = PositionState.CLOSED.value
        position.closed_at = _utc_now()
        position.close_reason = "emergency_unprotected"
        position.close_history_id = history.id
        position.current_quantity = Decimal("0")

        await self._emit_event(
            session=session,
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            history_id=history.id,
            position_id=position.id,
            event_type="position_emergency_closed_unprotected",
            level=EVENT_LEVEL_ERROR,
            message=(
                "Position was emergency-closed because protective orders could not be confirmed."
            ),
            payload={
                "symbol": position_context.symbol,
                "reason": reason,
                "close_error": close_error,
            },
            commit=False,
        )

    @staticmethod
    def _build_position_runtime_order_id(*, position_id: str, kind: str) -> str:
        return f"pos-{position_id}-{kind}-{int(datetime.now(UTC).timestamp() * 1000)}"[:64]

    async def get_config(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int | None = None,
        fail_on_ambiguous: bool = False,
    ) -> AutoTradeConfig | None:
        return await self._get_config_for_scope(
            session=session,
            user_id=user_id,
            account_id=account_id,
            fail_on_ambiguous=fail_on_ambiguous,
            lock_for_update=False,
        )

    async def list_configs(
        self,
        *,
        session: AsyncSession,
        user_id: int,
    ) -> list[AutoTradeConfig]:
        return list(
            (
                await session.scalars(
                    select(AutoTradeConfig)
                    .where(AutoTradeConfig.user_id == user_id)
                    .order_by(AutoTradeConfig.created_at.desc(), AutoTradeConfig.id.desc())
                )
            ).all()
        )

    async def upsert_config(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        payload: AutoTradeConfigUpsertRequest,
    ) -> AutoTradeConfig:
        profile = await session.scalar(
            select(PersonalAnalysisProfile).where(
                PersonalAnalysisProfile.id == payload.profile_id,
                PersonalAnalysisProfile.user_id == user_id,
            )
        )
        if profile is None:
            raise LookupError("Personal analysis profile not found.")
        try:
            to_linear_perp_symbol(profile.symbol)
        except ValueError as exc:
            raise ValueError(
                f"Unsupported profile symbol for linear futures: {profile.symbol}"
            ) from exc

        account = await self._credentials_service.get_account(
            session=session,
            account_id=payload.account_id,
            user_id=user_id,
        )
        if account.exchange_name not in _SUPPORTED_AUTO_TRADE_FUTURES_EXCHANGES:
            raise ValueError("Auto-trade futures v1 supports Bybit and Binance USDT-M only.")

        now = _utc_now()
        stmt: Select[tuple[AutoTradeConfig]] = select(AutoTradeConfig).where(
            AutoTradeConfig.user_id == user_id,
            AutoTradeConfig.account_id == payload.account_id,
        )
        stmt = self._with_for_update(session=session, stmt=stmt)
        row = cast(AutoTradeConfig | None, await session.scalar(stmt))

        if row is None:
            row = AutoTradeConfig(
                user_id=user_id,
                profile_id=payload.profile_id,
                account_id=payload.account_id,
                enabled=payload.enabled,
                is_running=False,
                position_size_usdt=float(payload.position_size_usdt),
                leverage=int(payload.leverage),
                min_confidence_pct=float(payload.min_confidence_pct),
                fast_close_confidence_pct=float(payload.fast_close_confidence_pct),
                confirm_reports_required=int(payload.confirm_reports_required),
                risk_mode=payload.risk_mode,
                sl_pct=float(payload.sl_pct),
                tp_pct=float(payload.tp_pct),
                strategy_profile_json=_serialize_strategy_profile(payload.strategy_profile),
                last_started_at=None,
                last_stopped_at=now if not payload.enabled else None,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            await self._emit_event(
                session=session,
                user_id=user_id,
                config_id=row.id,
                profile_id=row.profile_id,
                history_id=None,
                position_id=None,
                event_type="config_created",
                level=EVENT_LEVEL_INFO,
                message="Auto-trade config created.",
                payload={
                    "enabled": row.enabled,
                    "position_size_usdt": row.position_size_usdt,
                    "leverage": row.leverage,
                },
                commit=True,
            )
            return row

        requested_profile_change = row.profile_id != payload.profile_id
        requested_account_change = row.account_id != payload.account_id
        if requested_profile_change or requested_account_change:
            open_position: AutoTradePosition | None = None
            current_profile = cast(
                PersonalAnalysisProfile | None,
                await session.scalar(
                    select(PersonalAnalysisProfile).where(
                        PersonalAnalysisProfile.id == row.profile_id,
                        PersonalAnalysisProfile.user_id == user_id,
                    )
                ),
            )
            if current_profile is not None:
                try:
                    current_execution_symbol = to_linear_perp_symbol(current_profile.symbol)
                    open_position, _, _ = await self._sync_open_position_with_exchange(
                        session=session,
                        config=row,
                        execution_symbol=current_execution_symbol,
                        history_id=None,
                        emit_events=True,
                        close_missing_on_exchange=True,
                    )
                except (ExchangeServiceError, ValueError):
                    open_position = cast(
                        AutoTradePosition | None,
                        await session.scalar(
                            self._with_for_update(
                                session=session,
                                stmt=(
                                    select(AutoTradePosition)
                                    .where(
                                        AutoTradePosition.user_id == user_id,
                                        AutoTradePosition.account_id == row.account_id,
                                        AutoTradePosition.status == POSITION_OPEN,
                                    )
                                    .limit(1)
                                ),
                            )
                        ),
                    )
            if current_profile is None:
                open_position = cast(
                    AutoTradePosition | None,
                    await session.scalar(
                        self._with_for_update(
                            session=session,
                            stmt=(
                                select(AutoTradePosition)
                                .where(
                                    AutoTradePosition.user_id == user_id,
                                    AutoTradePosition.account_id == row.account_id,
                                    AutoTradePosition.status == POSITION_OPEN,
                                )
                                .limit(1)
                            ),
                        )
                    ),
                )
            if row.is_running:
                raise ValueError(
                    "Cannot change profile_id/account_id while auto-trade is running. "
                    "Stop auto-trade first."
                )
            if open_position is not None:
                raise ValueError(
                    "Cannot change profile_id/account_id while an auto-trade position is open."
                )

        row.profile_id = payload.profile_id
        row.account_id = payload.account_id
        row.enabled = bool(payload.enabled)
        if not row.enabled:
            row.is_running = False
            row.last_stopped_at = now
        row.position_size_usdt = float(payload.position_size_usdt)
        row.leverage = int(payload.leverage)
        row.min_confidence_pct = float(payload.min_confidence_pct)
        row.fast_close_confidence_pct = float(payload.fast_close_confidence_pct)
        row.confirm_reports_required = int(payload.confirm_reports_required)
        row.risk_mode = payload.risk_mode
        row.sl_pct = float(payload.sl_pct)
        row.tp_pct = float(payload.tp_pct)
        if "strategy_profile" in payload.model_fields_set:
            row.strategy_profile_json = _serialize_strategy_profile(payload.strategy_profile)
        await session.commit()
        await session.refresh(row)
        await self._emit_event(
            session=session,
            user_id=user_id,
            config_id=row.id,
            profile_id=row.profile_id,
            history_id=None,
            position_id=None,
            event_type="config_updated",
            level=EVENT_LEVEL_INFO,
            message="Auto-trade config updated.",
            payload={
                "enabled": row.enabled,
                "is_running": row.is_running,
                "position_size_usdt": row.position_size_usdt,
                "leverage": row.leverage,
            },
            commit=True,
        )
        return row

    async def set_running(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        is_running: bool,
        account_id: int | None = None,
    ) -> AutoTradeConfig:
        row = await self._get_config_for_scope(
            session=session,
            user_id=user_id,
            account_id=account_id,
            fail_on_ambiguous=True,
            lock_for_update=True,
        )
        if row is None:
            raise LookupError("Auto-trade config not found.")
        now = _utc_now()
        if is_running and not row.enabled:
            raise ValueError("Auto-trade config is disabled.")

        row.is_running = is_running
        if is_running:
            row.last_started_at = now
        else:
            row.last_stopped_at = now
        await session.commit()
        await session.refresh(row)
        await self._emit_event(
            session=session,
            user_id=user_id,
            config_id=row.id,
            profile_id=row.profile_id,
            history_id=None,
            position_id=None,
            event_type="auto_trade_play" if is_running else "auto_trade_stop",
            level=EVENT_LEVEL_INFO,
            message="Auto-trade started." if is_running else "Auto-trade stopped.",
            payload={"is_running": row.is_running},
            commit=True,
        )
        return row

    async def get_open_position(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int | None = None,
    ) -> AutoTradePosition | None:
        config = await self.get_config(
            session=session,
            user_id=user_id,
            account_id=account_id,
            fail_on_ambiguous=True,
        )
        if config is None:
            return await self._get_latest_open_position(
                session=session,
                user_id=user_id,
                account_id=account_id,
            )
        profile = cast(
            PersonalAnalysisProfile | None,
            await session.scalar(
                select(PersonalAnalysisProfile).where(
                    PersonalAnalysisProfile.id == config.profile_id,
                    PersonalAnalysisProfile.user_id == user_id,
                )
            ),
        )
        if profile is None:
            return await self._get_latest_open_position(
                session=session,
                user_id=user_id,
                account_id=account_id,
            )
        try:
            execution_symbol = to_linear_perp_symbol(profile.symbol)
        except ValueError:
            return await self._get_latest_open_position(
                session=session,
                user_id=user_id,
                account_id=account_id,
            )

        try:
            position, changed, _ = await self._sync_open_position_with_exchange(
                session=session,
                config=config,
                execution_symbol=execution_symbol,
                history_id=None,
                emit_events=True,
                close_missing_on_exchange=True,
            )
        except ExchangeServiceError:
            return await self._get_latest_open_position(
                session=session,
                user_id=user_id,
                account_id=account_id,
            )
        if changed and position is not None:
            await session.commit()
            await session.refresh(position)
        return position

    async def list_positions(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        limit: int,
        status: str | None = None,
        account_id: int | None = None,
    ) -> list[AutoTradePosition]:
        if account_id is None:
            await self.get_config(
                session=session,
                user_id=user_id,
                account_id=None,
                fail_on_ambiguous=True,
            )
        await self._sync_positions_snapshot_for_user(
            session=session,
            user_id=user_id,
            account_id=account_id,
            history_id=None,
            emit_events=True,
            close_missing_on_exchange=True,
        )
        stmt = select(AutoTradePosition).where(AutoTradePosition.user_id == user_id)
        if account_id is not None:
            stmt = stmt.where(AutoTradePosition.account_id == account_id)
        if status is not None:
            stmt = stmt.where(AutoTradePosition.status == status)
        rows = await session.scalars(
            stmt.order_by(
                desc(AutoTradePosition.opened_at),
                desc(AutoTradePosition.id),
            ).limit(limit)
        )
        return list(rows.all())

    async def build_position_pnl_snapshot(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        position: AutoTradePosition,
    ) -> dict[str, Any]:
        chart_symbol = self._safe_chart_symbol(position.symbol)
        calculated_at = _utc_now()
        fees_usdt = await self._fetch_position_fees_usdt(
            session=session,
            user_id=user_id,
            position=position,
        )

        if position.status != POSITION_OPEN:
            entry_notional = float(position.entry_price) * float(position.quantity)
            initial_margin = entry_notional / max(float(position.leverage), 1.0)
            close_price = self._positive_or_none(position.close_price)
            inferred = None
            if close_price is None:
                inferred = await self._infer_closed_position_from_trades(
                    session=session,
                    user_id=user_id,
                    position=position,
                )
                if inferred is not None:
                    close_price = inferred["close_price"]
            realized = (
                self._directional_pnl(
                    side=position.side,
                    entry_price=float(position.entry_price),
                    mark_price=close_price,
                    quantity=float(position.quantity),
                )
                if close_price is not None
                else None
            )
            if inferred is not None and inferred["realized_pnl_usdt"] is not None:
                realized = float(inferred["realized_pnl_usdt"])
            elif realized is not None and fees_usdt:
                realized = float(realized) - fees_usdt
            total = realized
            pnl_pct = self._ratio_percent(total, entry_notional)
            roe_pct = self._ratio_percent(total, initial_margin)
            return {
                "position_id": position.id,
                "symbol": position.symbol,
                "chart_symbol": chart_symbol,
                "side": position.side,
                "status": position.status,
                "entry_price": float(position.entry_price),
                "mark_price": close_price,
                "close_price": close_price,
                "quantity": float(position.quantity),
                "entry_notional_usdt": entry_notional,
                "initial_margin_usdt": initial_margin,
                "realized_pnl_usdt": realized,
                "unrealized_pnl_usdt": 0.0 if realized is not None else None,
                "total_pnl_usdt": total,
                "pnl_pct": pnl_pct,
                "roe_pct": roe_pct,
                "source": "closed" if inferred is None and realized is not None else (
                    "derived" if realized is not None else "unavailable"
                ),
                "error": None,
                "calculated_at": calculated_at,
            }

        mark_price: float | None = None
        unrealized: float | None = None
        source = "unavailable"
        error: str | None = None
        live_entry_price = float(position.entry_price)
        live_quantity = float(position.quantity)
        try:
            live_position = await self._trading.fetch_futures_position(
                session=session,
                user_id=user_id,
                account_id=position.account_id,
                symbol=position.symbol,
            )
            if live_position is not None:
                if live_position.contracts > _POSITION_EPSILON:
                    live_quantity = float(live_position.contracts)
                normalized_entry = self._positive_or_none(live_position.entry_price)
                if normalized_entry is not None:
                    live_entry_price = normalized_entry
                mark_price = self._positive_or_none(live_position.mark_price)
                if live_position.unrealized_pnl is not None and math.isfinite(
                    float(live_position.unrealized_pnl)
                ):
                    unrealized = float(live_position.unrealized_pnl)
                    source = "exchange"
                elif mark_price is not None:
                    unrealized = self._directional_pnl(
                        side=position.side,
                        entry_price=live_entry_price,
                        mark_price=mark_price,
                        quantity=live_quantity,
                    )
                    source = "derived"
        except Exception as exc:
            error = str(exc)

        entry_notional = live_entry_price * live_quantity
        initial_margin = entry_notional / max(float(position.leverage), 1.0)
        realized = -fees_usdt if unrealized is not None and fees_usdt else 0.0
        total = unrealized + realized if unrealized is not None else None
        pnl_pct = self._ratio_percent(total, entry_notional)
        roe_pct = self._ratio_percent(total, initial_margin)
        return {
            "position_id": position.id,
            "symbol": position.symbol,
            "chart_symbol": chart_symbol,
            "side": position.side,
            "status": position.status,
            "entry_price": live_entry_price,
            "mark_price": mark_price,
            "close_price": None,
            "quantity": live_quantity,
            "entry_notional_usdt": entry_notional,
            "initial_margin_usdt": initial_margin,
            "realized_pnl_usdt": realized if unrealized is not None else None,
            "unrealized_pnl_usdt": unrealized,
            "total_pnl_usdt": total,
            "pnl_pct": pnl_pct,
            "roe_pct": roe_pct,
            "source": source,
            "error": error,
            "calculated_at": calculated_at,
        }

    async def summarize_positions_pnl(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        limit: int,
        status: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        positions = await self.list_positions(
            session=session,
            user_id=user_id,
            limit=limit,
            status=status,
            account_id=account_id,
        )
        rows: list[dict[str, Any]] = []
        realized_total = 0.0
        unrealized_total = 0.0
        trade_total = 0.0
        open_count = 0
        closed_count = 0
        for position in positions:
            pnl = await self.build_position_pnl_snapshot(
                session=session,
                user_id=user_id,
                position=position,
            )
            if position.status == POSITION_OPEN:
                open_count += 1
            elif position.status == POSITION_CLOSED:
                closed_count += 1

            realized = pnl.get("realized_pnl_usdt")
            unrealized = pnl.get("unrealized_pnl_usdt")
            if isinstance(realized, (int, float)) and math.isfinite(float(realized)):
                realized_total += float(realized)
            if isinstance(unrealized, (int, float)) and math.isfinite(float(unrealized)):
                unrealized_total += float(unrealized)
            trade_pnl: float | None = None
            if position.status == POSITION_CLOSED:
                closed_total = pnl.get("total_pnl_usdt")
                if isinstance(closed_total, (int, float)) and math.isfinite(float(closed_total)):
                    trade_pnl = float(closed_total)
                    trade_total += trade_pnl
            rows.append(
                {
                    "position": position,
                    "pnl": pnl,
                    "lifecycle": self._build_position_lifecycle(position=position),
                    "trade_pnl_usdt": trade_pnl,
                }
            )

        return {
            "positions": rows,
            "summary": {
                "total_positions": len(positions),
                "open_positions": open_count,
                "closed_positions": closed_count,
                "total_realized_pnl_usdt": realized_total,
                "total_unrealized_pnl_usdt": unrealized_total,
                "total_pnl_usdt": realized_total + unrealized_total,
                "total_trade_pnl_usdt": trade_total,
            },
        }

    @staticmethod
    def _build_position_lifecycle(position: AutoTradePosition) -> dict[str, Any]:
        opened_at = position.opened_at
        closed_at = position.closed_at
        duration_seconds: int | None = None
        if closed_at is not None:
            delta_seconds = int((closed_at - opened_at).total_seconds())
            duration_seconds = max(delta_seconds, 0)
        return {
            "entry": {
                "time": opened_at,
                "price": float(position.entry_price),
                "quantity": float(position.quantity),
            },
            "exit": {
                "time": closed_at,
                "price": float(position.close_price) if position.close_price is not None else None,
                "reason": position.close_reason,
            },
            "is_closed": position.status == POSITION_CLOSED,
            "duration_seconds": duration_seconds,
        }

    async def _sync_positions_snapshot_for_user(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int | None,
        history_id: int | None,
        emit_events: bool,
        close_missing_on_exchange: bool,
    ) -> None:
        changed = False
        stmt = select(AutoTradeConfig).where(AutoTradeConfig.user_id == user_id)
        if account_id is not None:
            stmt = stmt.where(AutoTradeConfig.account_id == account_id)
        configs = list((await session.scalars(stmt)).all())
        for config in configs:
            profile = cast(
                PersonalAnalysisProfile | None,
                await session.scalar(
                    select(PersonalAnalysisProfile).where(
                        PersonalAnalysisProfile.id == config.profile_id,
                        PersonalAnalysisProfile.user_id == user_id,
                    )
                ),
            )
            if profile is None:
                continue
            try:
                execution_symbol = to_linear_perp_symbol(profile.symbol)
            except ValueError:
                continue
            try:
                _, config_changed, _ = await self._sync_open_position_with_exchange(
                    session=session,
                    config=config,
                    execution_symbol=execution_symbol,
                    history_id=history_id,
                    emit_events=emit_events,
                    close_missing_on_exchange=close_missing_on_exchange,
                )
            except ExchangeServiceError:
                continue
            changed = changed or config_changed
        normalized_any = False
        rows = list(
            (
                await session.scalars(
                    select(AutoTradePosition).where(AutoTradePosition.user_id == user_id)
                )
            ).all()
        )
        if account_id is not None:
            rows = [row for row in rows if row.account_id == account_id]
        for row in rows:
            normalized_any = self._normalize_legacy_closed_position(row) or normalized_any
        if changed or normalized_any:
            await session.commit()

    async def get_signal_state(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int | None = None,
    ) -> AutoTradeSignalState | None:
        config = await self.get_config(
            session=session,
            user_id=user_id,
            account_id=account_id,
            fail_on_ambiguous=True,
        )
        if config is None:
            return None
        return cast(
            AutoTradeSignalState | None,
            await session.scalar(
                select(AutoTradeSignalState)
                .where(AutoTradeSignalState.config_id == config.id)
                .limit(1)
            ),
        )

    async def list_events(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        limit: int,
        account_id: int | None = None,
    ) -> list[AutoTradeEvent]:
        stmt = select(AutoTradeEvent).where(AutoTradeEvent.user_id == user_id)
        if account_id is not None:
            config = await self.get_config(
                session=session,
                user_id=user_id,
                account_id=account_id,
                fail_on_ambiguous=True,
            )
            if config is None:
                return []
            stmt = stmt.where(AutoTradeEvent.config_id == config.id)
        else:
            await self.get_config(
                session=session,
                user_id=user_id,
                account_id=None,
                fail_on_ambiguous=True,
            )
        rows = await session.scalars(
            stmt.order_by(AutoTradeEvent.created_at.desc()).limit(limit)
        )
        return list(rows.all())

    async def enqueue_history_signal(
        self,
        *,
        session: AsyncSession,
        history: PersonalAnalysisHistory,
    ) -> bool:
        configs = list(
            (
                await session.scalars(
                    select(AutoTradeConfig).where(
                        AutoTradeConfig.user_id == history.user_id,
                        AutoTradeConfig.profile_id == history.profile_id,
                        AutoTradeConfig.enabled.is_(True),
                        AutoTradeConfig.is_running.is_(True),
                    )
                )
            ).all()
        )
        if not configs:
            return False

        enqueued_any = False
        for config in configs:
            queue_row = AutoTradeSignalQueue(
                user_id=history.user_id,
                config_id=config.id,
                profile_id=history.profile_id,
                history_id=history.id,
                status=QUEUE_PENDING,
                attempt=0,
                max_attempts=self._max_attempts,
                next_retry_at=_utc_now(),
                locked_at=None,
                processed_at=None,
                last_error=None,
            )
            try:
                async with session.begin_nested():
                    session.add(queue_row)
                    await session.flush()
            except IntegrityError:
                continue

            enqueued_any = True
            await self._emit_event(
                session=session,
                user_id=history.user_id,
                config_id=config.id,
                profile_id=history.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_enqueued",
                level=EVENT_LEVEL_INFO,
                message="Signal enqueued for auto-trade processing.",
                payload={"history_id": history.id, "account_id": config.account_id},
                commit=False,
            )
        return enqueued_any

    async def process_signal_queue(self, *, session: AsyncSession) -> dict[str, int]:
        if not self._scheduler_loop_enabled:
            return {
                "polled": 0,
                "completed": 0,
                "skipped": 0,
                "retried": 0,
                "dead": 0,
                "errors": 0,
            }

        now = _utc_now()
        stats = {
            "polled": 0,
            "completed": 0,
            "skipped": 0,
            "retried": 0,
            "dead": 0,
            "errors": 0,
        }

        while True:
            stmt: Select[tuple[AutoTradeSignalQueue]] = (
                select(AutoTradeSignalQueue)
                .where(
                    AutoTradeSignalQueue.status.in_(_QUEUE_ACTIVE_STATUSES),
                    AutoTradeSignalQueue.next_retry_at <= now,
                )
                .order_by(AutoTradeSignalQueue.next_retry_at.asc(), AutoTradeSignalQueue.id.asc())
                .limit(self._status_batch_size)
            )
            stmt = self._with_for_update_skip_locked(session=session, stmt=stmt)
            queue_items = list((await session.scalars(stmt)).all())
            if not queue_items:
                break

            stats["polled"] += len(queue_items)
            for queue_item in queue_items:
                queue_item.status = QUEUE_PROCESSING
                queue_item.locked_at = now
                queue_item.attempt += 1
                try:
                    outcome = await self._process_queue_item(
                        session=session,
                        queue_item=queue_item,
                        now=now,
                    )
                    if outcome == "skipped":
                        stats["skipped"] += 1
                    elif outcome == "completed":
                        stats["completed"] += 1
                    elif outcome == "dead":
                        stats["dead"] += 1
                except (ExchangeServiceError, AnalysisProviderError) as exc:
                    retried = self._mark_retry_or_dead(
                        queue_item=queue_item,
                        now=now,
                        error=str(exc),
                        retryable=getattr(exc, "retryable", True),
                    )
                    if retried:
                        stats["retried"] += 1
                    else:
                        stats["dead"] += 1
                    stats["errors"] += 1
                    await self._emit_event(
                        session=session,
                        user_id=queue_item.user_id,
                        config_id=queue_item.config_id,
                        profile_id=queue_item.profile_id,
                        history_id=queue_item.history_id,
                        position_id=None,
                        event_type="signal_process_error",
                        level=EVENT_LEVEL_ERROR,
                        message=str(exc),
                        payload={"queue_id": queue_item.id, "attempt": queue_item.attempt},
                        commit=False,
                    )
                except Exception as exc:
                    retried = self._mark_retry_or_dead(
                        queue_item=queue_item,
                        now=now,
                        error=str(exc),
                        retryable=False,
                    )
                    if retried:
                        stats["retried"] += 1
                    else:
                        stats["dead"] += 1
                    stats["errors"] += 1
                    await self._emit_event(
                        session=session,
                        user_id=queue_item.user_id,
                        config_id=queue_item.config_id,
                        profile_id=queue_item.profile_id,
                        history_id=queue_item.history_id,
                        position_id=None,
                        event_type="signal_process_unexpected_error",
                        level=EVENT_LEVEL_ERROR,
                        message=str(exc),
                        payload={"queue_id": queue_item.id, "attempt": queue_item.attempt},
                        commit=False,
                    )

            if len(queue_items) < self._status_batch_size:
                break

        await session.commit()
        return stats

    async def _process_queue_item(
        self,
        *,
        session: AsyncSession,
        queue_item: AutoTradeSignalQueue,
        now: datetime,
    ) -> str:
        config = await self._get_config_for_update(
            session=session,
            config_id=queue_item.config_id,
        )
        if config is None:
            queue_item.status = QUEUE_DEAD
            queue_item.last_error = "Auto-trade config not found."
            queue_item.processed_at = now
            queue_item.locked_at = None
            return "dead"

        if config.id != queue_item.config_id:
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=queue_item.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=queue_item.history_id,
                position_id=None,
                event_type="signal_skipped_stale_config",
                level=EVENT_LEVEL_WARNING,
                message="Queue item has stale config reference.",
                payload={"queue_id": queue_item.id},
                commit=False,
            )
            return "skipped"

        if not config.enabled or not config.is_running:
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=queue_item.history_id,
                position_id=None,
                event_type="signal_skipped_config_inactive",
                level=EVENT_LEVEL_INFO,
                message="Auto-trade config is not active.",
                payload={"enabled": config.enabled, "is_running": config.is_running},
                commit=False,
            )
            return "skipped"

        if config.profile_id != queue_item.profile_id:
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=queue_item.history_id,
                position_id=None,
                event_type="signal_skipped_profile_mismatch",
                level=EVENT_LEVEL_WARNING,
                message="Queue item profile does not match current config.",
                payload={
                    "queue_profile_id": queue_item.profile_id,
                    "config_profile_id": config.profile_id,
                },
                commit=False,
            )
            return "skipped"

        history = await session.get(PersonalAnalysisHistory, queue_item.history_id)
        if history is None:
            queue_item.status = QUEUE_DEAD
            queue_item.last_error = "Personal analysis history not found."
            queue_item.processed_at = now
            queue_item.locked_at = None
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=queue_item.history_id,
                position_id=None,
                event_type="signal_dead_history_not_found",
                level=EVENT_LEVEL_ERROR,
                message="History row not found for queued signal.",
                payload={"queue_id": queue_item.id},
                commit=False,
            )
            return "dead"

        state = await self._get_or_create_signal_state(
            session=session,
            config=config,
        )
        if history.id <= state.last_processed_history_id:
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_skipped_duplicate_history",
                level=EVENT_LEVEL_INFO,
                message="History already processed.",
                payload={"last_processed_history_id": state.last_processed_history_id},
                commit=False,
            )
            return "skipped"

        try:
            raw_payload = cast(dict[str, Any], history.analysis_data)
            normalized_payload = adapt_legacy_analysis_structured_payload(
                payload=raw_payload,
                history_symbol=history.symbol,
                core_completed_at=history.core_completed_at,
                history_created_at=history.created_at,
            )
            signal = parse_auto_trade_signal(normalized_payload)
        except ValueError as exc:
            state.last_processed_history_id = max(state.last_processed_history_id, history.id)
            state.last_signal_at = history.core_completed_at or history.created_at
            state.last_signal_confidence_pct = None
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_skipped_invalid_payload",
                level=EVENT_LEVEL_WARNING,
                message=str(exc),
                payload={"queue_id": queue_item.id},
                commit=False,
            )
            return "skipped"

        profile = cast(
            PersonalAnalysisProfile | None,
            await session.scalar(
                select(PersonalAnalysisProfile).where(
                    PersonalAnalysisProfile.id == config.profile_id,
                    PersonalAnalysisProfile.user_id == config.user_id,
                )
            ),
        )
        if profile is None:
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_skipped_profile_not_found",
                level=EVENT_LEVEL_ERROR,
                message="Configured profile not found.",
                payload={"profile_id": config.profile_id},
                commit=False,
            )
            return "skipped"

        try:
            signal_symbol_key = symbol_market_key(signal.symbol)
        except ValueError as exc:
            state.last_processed_history_id = max(state.last_processed_history_id, history.id)
            state.last_signal_at = signal.generated_at
            state.last_signal_confidence_pct = signal.confidence_pct
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_skipped_invalid_payload",
                level=EVENT_LEVEL_WARNING,
                message=str(exc),
                payload={"queue_id": queue_item.id, "field": "symbol"},
                commit=False,
            )
            return "skipped"

        try:
            profile_symbol_key = symbol_market_key(profile.symbol)
            execution_symbol = to_linear_perp_symbol(profile.symbol)
        except ValueError as exc:
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_skipped_invalid_profile_symbol",
                level=EVENT_LEVEL_ERROR,
                message=str(exc),
                payload={
                    "queue_id": queue_item.id,
                    "profile_symbol": profile.symbol,
                },
                commit=False,
            )
            return "skipped"

        if signal_symbol_key != profile_symbol_key:
            state.last_processed_history_id = max(state.last_processed_history_id, history.id)
            state.last_signal_at = signal.generated_at
            state.last_signal_confidence_pct = signal.confidence_pct
            self._mark_completed(queue_item=queue_item, now=now, error=None)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_skipped_symbol_mismatch",
                level=EVENT_LEVEL_WARNING,
                message="Signal symbol does not match configured profile symbol.",
                payload={
                    "signal_symbol": signal.symbol,
                    "profile_symbol": profile.symbol,
                },
                commit=False,
            )
            return "skipped"

        open_position, _, exchange_position = await self._sync_open_position_with_exchange(
            session=session,
            config=config,
            execution_symbol=execution_symbol,
            history_id=history.id,
            emit_events=True,
            close_missing_on_exchange=False,
        )

        if open_position is None:
            await self._process_without_open_position(
                session=session,
                config=config,
                state=state,
                signal=signal,
                history=history,
                execution_symbol=execution_symbol,
            )
        else:
            await self._process_with_open_position(
                session=session,
                config=config,
                state=state,
                signal=signal,
                history=history,
                position=open_position,
                exchange_position=exchange_position,
            )

        state.last_processed_history_id = max(state.last_processed_history_id, history.id)
        state.last_signal_at = signal.generated_at
        state.last_signal_confidence_pct = signal.confidence_pct
        self._mark_completed(queue_item=queue_item, now=now, error=None)
        return "completed"

    async def _process_without_open_position(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        state: AutoTradeSignalState,
        signal: ParsedAutoTradeSignal,
        history: PersonalAnalysisHistory,
        execution_symbol: str,
    ) -> None:
        if signal.trend == TREND_NEUTRAL:
            state.last_trend = TREND_NEUTRAL
            state.opposite_streak = 0
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_neutral_no_action",
                level=EVENT_LEVEL_INFO,
                message="Neutral trend. No position opened.",
                payload={"trend": signal.trend, "confidence_pct": signal.confidence_pct},
                commit=False,
            )
            return

        if signal.confidence_pct < config.min_confidence_pct:
            state.last_trend = signal.trend
            state.opposite_streak = 0
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_below_min_confidence",
                level=EVENT_LEVEL_INFO,
                message="Signal confidence below minimum threshold.",
                payload={
                    "trend": signal.trend,
                    "confidence_pct": signal.confidence_pct,
                    "min_confidence_pct": config.min_confidence_pct,
                },
                commit=False,
            )
            return

        quantity = config.position_size_usdt / signal.price_current
        if quantity <= 0:
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_invalid_position_size",
                level=EVENT_LEVEL_ERROR,
                message="Computed quantity is non-positive.",
                payload={
                    "position_size_usdt": config.position_size_usdt,
                    "price": signal.price_current,
                },
                commit=False,
            )
            return

        tp_price, sl_price = self._calculate_tp_sl(signal=signal, config=config)
        entry_order_side = (
            ExchangeOrderSide.BUY if signal.trend == TREND_LONG else ExchangeOrderSide.SELL
        )
        open_client_order_id = self._build_client_order_id(
            prefix="at-open",
            user_id=config.user_id,
            config_id=config.id,
            history_id=history.id,
        )
        exchange_name = await self._resolve_account_exchange_name(
            session=session,
            account_id=config.account_id,
        )
        position_context = self._build_position_context(
            config=config,
            signal=signal,
            exchange_name=exchange_name,
            execution_symbol=execution_symbol,
            quantity=quantity,
            tp_price=tp_price,
            sl_price=sl_price,
        )
        position_context.state = position_context.state_machine.transition(
            TransitionTrigger.ENTRY_SUBMITTED,
            reason="Auto-trade entry submitted",
            metadata={
                "history_id": history.id,
                "client_order_id": open_client_order_id,
            },
        )

        await self._trading.set_futures_leverage(
            session=session,
            user_id=config.user_id,
            account_id=config.account_id,
            symbol=execution_symbol,
            leverage=config.leverage,
        )

        opened, entry_adapter = await self._place_entry_order(
            session=session,
            config=config,
            position=position_context,
            side=entry_order_side,
            quantity=quantity,
            client_order_id=open_client_order_id,
            take_profit_price=tp_price,
            stop_loss_price=sl_price,
        )
        entry_price = float(
            opened.average_price or opened.price or position_context.entry_price or signal.price_current
        )
        filled_quantity = (
            float(opened.filled_quantity)
            if opened.filled_quantity > _POSITION_EPSILON
            else quantity
        )
        self._refresh_runtime_prices_after_fill(
            position=position_context,
            config=config,
            trend=signal.trend,
            entry_price=entry_price,
            filled_quantity=filled_quantity,
        )
        position_context.state = position_context.state_machine.transition(
            TransitionTrigger.ENTRY_FILLED,
            reason="Auto-trade entry filled",
            metadata={
                "exchange_order_id": opened.exchange_order_id,
                "filled_quantity": filled_quantity,
            },
        )

        position = AutoTradePosition(
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            account_id=config.account_id,
            symbol=execution_symbol,
            side=signal.trend,
            status=POSITION_OPEN,
            entry_price=entry_price,
            quantity=filled_quantity,
            position_size_usdt=config.position_size_usdt,
            leverage=config.leverage,
            tp_price=float(self._resolve_runtime_tp_price(position_context) or tp_price),
            sl_price=float(position_context.current_sl_price),
            entry_confidence_pct=signal.confidence_pct,
            opened_at=self._parse_optional_datetime(position_context.opened_at) or _utc_now(),
            closed_at=None,
            close_reason=None,
            close_price=None,
            open_order_id=opened.exchange_order_id,
            close_order_id=None,
            open_history_id=history.id,
            close_history_id=None,
            raw_open_order=opened.raw if isinstance(opened.raw, dict) else {},
            raw_close_order={},
        )
        session.add(position)
        await session.flush()
        position_context.position_id = str(position.id)
        position_context.state_machine.position_id = position_context.position_id
        self._merge_position_context_into_row(row=position, position=position_context)
        await self._record_order_metadata(
            session=session,
            user_id=config.user_id,
            account_id=config.account_id,
            order_id=opened.exchange_order_id,
            client_order_id=opened.client_order_id,
            symbol=execution_symbol,
            source="auto_trade_open",
            config_id=config.id,
            position_id=position.id,
            history_id=history.id,
        )

        runtime_integration_error: str | None = None
        try:
            await self._initialize_position_runtime(
                session=session,
                position=position_context,
                adapter=entry_adapter,
                opened=opened,
            )
        except Exception as exc:
            runtime_integration_error = str(exc)

        sl_unprotected = (
            opened.attached_sl is None
            and not position_context.sl_exchange_order_id
        )
        if runtime_integration_error is not None and sl_unprotected:
            await self._emergency_close_unprotected_position(
                session=session,
                config=config,
                position=position,
                position_context=position_context,
                adapter=entry_adapter,
                history=history,
                reason=f"sl_init_failed:{runtime_integration_error}",
                filled_quantity=filled_quantity,
            )
            return

        state.last_trend = signal.trend
        state.opposite_streak = 0
        await self._emit_event(
            session=session,
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            history_id=history.id,
            position_id=position.id,
            event_type="position_opened",
            level=EVENT_LEVEL_INFO,
            message="Position opened from signal.",
            payload={
                "symbol": signal.symbol,
                "execution_symbol": execution_symbol,
                "trend": signal.trend,
                "entry_price": entry_price,
                "quantity": filled_quantity,
                "tp_price": float(self._resolve_runtime_tp_price(position_context) or tp_price),
                "sl_price": float(position_context.current_sl_price),
                "confidence_pct": signal.confidence_pct,
                "state": position_context.state.value,
                "bracket_sl_attached": opened.attached_sl is not None,
                "bracket_tp_attached": opened.attached_tp is not None,
            },
            commit=False,
        )
        if runtime_integration_error is not None:
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=position.id,
                event_type="position_runtime_integration_warning",
                level=EVENT_LEVEL_WARNING,
                message="Position opened but runtime integrations were only partially initialized.",
                payload={"error": runtime_integration_error},
                commit=False,
            )

    async def _process_with_open_position(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        state: AutoTradeSignalState,
        signal: ParsedAutoTradeSignal,
        history: PersonalAnalysisHistory,
        position: AutoTradePosition,
        exchange_position: NormalizedFuturesPosition | None,
    ) -> None:
        if self._is_position_closed_on_exchange(exchange_position):
            position.status = POSITION_CLOSED
            position.state = PositionState.CLOSED.value
            position.closed_at = _utc_now()
            position.close_reason = "already_closed_on_exchange"
            position.close_history_id = history.id
            position.current_quantity = Decimal("0")
            position.close_price = (
                float(exchange_position.mark_price)
                if exchange_position is not None and exchange_position.mark_price is not None
                else None
            )
            state.last_trend = signal.trend
            state.opposite_streak = 0
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=position.id,
                event_type="position_marked_closed_from_exchange_state",
                level=EVENT_LEVEL_WARNING,
                message="Position was already closed on exchange.",
                payload={"symbol": position.symbol},
                commit=False,
            )
            return

        if signal.trend == TREND_NEUTRAL:
            state.last_trend = TREND_NEUTRAL
            state.opposite_streak = 0
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=position.id,
                event_type="open_position_neutral_hold",
                level=EVENT_LEVEL_INFO,
                message="Neutral trend while position is open. No action.",
                payload={"position_side": position.side},
                commit=False,
            )
            return

        if signal.trend == position.side:
            state.last_trend = signal.trend
            state.opposite_streak = 0
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=position.id,
                event_type="open_position_same_trend_hold",
                level=EVENT_LEVEL_INFO,
                message="Signal trend matches open position. Holding.",
                payload={"trend": signal.trend, "confidence_pct": signal.confidence_pct},
                commit=False,
            )
            return

        should_close = False
        close_reason = "opposite_confirmed"
        if signal.confidence_pct >= config.fast_close_confidence_pct:
            should_close = True
            close_reason = "opposite_fast_confidence"
        else:
            state.opposite_streak += 1
            should_close = state.opposite_streak >= config.confirm_reports_required
            if not should_close:
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=history.id,
                    position_id=position.id,
                    event_type="open_position_opposite_waiting_confirmation",
                    level=EVENT_LEVEL_INFO,
                    message="Opposite trend detected, waiting for confirmation.",
                    payload={
                        "opposite_streak": state.opposite_streak,
                        "required": config.confirm_reports_required,
                        "confidence_pct": signal.confidence_pct,
                    },
                    commit=False,
                )
                state.last_trend = signal.trend
                return

        if exchange_position is None:
            raise ExchangeServiceError(
                code="temporary_unavailable",
                message="Exchange position snapshot missing for open position.",
                retryable=True,
            )

        close_amount = float(exchange_position.contracts)
        if close_amount <= _POSITION_EPSILON:
            raise ExchangeServiceError(
                code="temporary_unavailable",
                message="Exchange returned non-positive contracts for open position.",
                retryable=True,
            )

        close_side: OrderSide = "sell" if position.side == TREND_LONG else "buy"
        close_client_order_id = self._build_client_order_id(
            prefix="at-close",
            user_id=config.user_id,
            config_id=config.id,
            history_id=history.id,
        )
        closed = await self._trading.close_futures_market_reduce_only(
            session=session,
            user_id=config.user_id,
            account_id=position.account_id,
            symbol=position.symbol,
            side=close_side,
            amount=close_amount,
            client_order_id=close_client_order_id,
        )
        close_order = closed.order
        close_price = float(close_order.average or close_order.price or signal.price_current)
        confirmed_closed, residual_position = await self._confirm_position_closed(
            session=session,
            user_id=config.user_id,
            account_id=position.account_id,
            symbol=position.symbol,
        )
        if not confirmed_closed:
            raise ExchangeServiceError(
                code="temporary_unavailable",
                message=(
                    "Close order submitted but exchange still reports open contracts. "
                    "Will retry confirmation."
                ),
                retryable=True,
            )

        position.status = POSITION_CLOSED
        position.state = PositionState.CLOSED.value
        position.closed_at = _utc_now()
        position.close_reason = close_reason
        position.close_price = close_price
        position.close_order_id = close_order.id
        position.close_history_id = history.id
        position.current_quantity = Decimal("0")
        position.raw_close_order = close_order.raw if isinstance(close_order.raw, dict) else {}
        await self._record_order_metadata(
            session=session,
            user_id=config.user_id,
            account_id=position.account_id,
            order_id=close_order.id,
            client_order_id=close_order.client_order_id,
            symbol=position.symbol,
            source="auto_trade_close",
            config_id=config.id,
            position_id=position.id,
            history_id=history.id,
        )

        state.last_trend = signal.trend
        state.opposite_streak = 0
        await self._emit_event(
            session=session,
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            history_id=history.id,
            position_id=position.id,
            event_type="position_closed_on_opposite_trend",
            level=EVENT_LEVEL_INFO,
            message="Position closed due to opposite trend.",
            payload={
                "close_reason": close_reason,
                "close_price": close_price,
                "signal_confidence_pct": signal.confidence_pct,
                "position_side": position.side,
                "signal_trend": signal.trend,
                "close_amount": close_amount,
                "residual_contracts": (
                    residual_position.contracts if residual_position is not None else 0.0
                ),
            },
            commit=False,
        )

    @staticmethod
    def _is_position_closed_on_exchange(position: NormalizedFuturesPosition | None) -> bool:
        if position is None:
            return True
        return float(position.contracts) <= _POSITION_EPSILON

    async def _sync_open_position_with_exchange(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        execution_symbol: str,
        history_id: int | None,
        emit_events: bool,
        close_missing_on_exchange: bool,
    ) -> tuple[AutoTradePosition | None, bool, NormalizedFuturesPosition | None]:
        db_open_rows = list(
            (
                await session.scalars(
                    self._with_for_update(
                        session=session,
                        stmt=(
                            select(AutoTradePosition)
                            .where(
                                AutoTradePosition.user_id == config.user_id,
                                AutoTradePosition.account_id == config.account_id,
                                AutoTradePosition.status == POSITION_OPEN,
                            )
                            .order_by(desc(AutoTradePosition.id))
                        ),
                    )
                )
            ).all()
        )
        db_open_position = db_open_rows[0] if db_open_rows else None
        duplicate_open_rows = db_open_rows[1:]
        exchange_position = await self._trading.fetch_futures_position(
            session=session,
            user_id=config.user_id,
            account_id=config.account_id,
            symbol=execution_symbol,
        )
        if self._is_position_closed_on_exchange(exchange_position):
            if db_open_position is None:
                return None, False, exchange_position
            if not close_missing_on_exchange:
                return db_open_position, False, exchange_position
            now = _utc_now()
            closed_count = 0
            for row in db_open_rows:
                row.status = POSITION_CLOSED
                row.state = PositionState.CLOSED.value
                row.closed_at = now
                row.close_reason = "already_closed_on_exchange"
                row.close_history_id = history_id
                row.current_quantity = Decimal("0")
                if self._normalize_legacy_closed_position(row):
                    pass
                closed_count += 1
            mark_price = (
                self._positive_or_none(exchange_position.mark_price)
                if exchange_position is not None
                else None
            )
            if mark_price is not None:
                for row in db_open_rows:
                    row.close_price = mark_price
            return None, True, exchange_position

        side = exchange_position.side
        live_side = TREND_LONG if side == "long" else TREND_SHORT
        live_contracts = float(exchange_position.contracts)
        live_entry_price = self._positive_or_none(exchange_position.entry_price)
        live_mark_price = self._positive_or_none(exchange_position.mark_price)
        live_take_profit = self._positive_or_none(exchange_position.take_profit_price)
        live_stop_loss = self._positive_or_none(exchange_position.stop_loss_price)
        resolved_entry_price = live_entry_price or live_mark_price
        live_leverage = self._positive_or_none(exchange_position.leverage)
        resolved_leverage = max(1, int(round(live_leverage))) if live_leverage else config.leverage

        if db_open_position is None:
            if resolved_entry_price is None:
                if emit_events:
                    await self._emit_event(
                        session=session,
                        user_id=config.user_id,
                        config_id=config.id,
                        profile_id=config.profile_id,
                        history_id=history_id,
                        position_id=None,
                        event_type="position_sync_skipped_missing_entry_price",
                        level=EVENT_LEVEL_WARNING,
                        message="Exchange returned open contracts but entry/mark price is missing.",
                        payload={"symbol": execution_symbol, "contracts": live_contracts},
                        commit=False,
                    )
                return None, False, exchange_position
            position_size_usdt = resolved_entry_price * live_contracts
            if not math.isfinite(position_size_usdt) or position_size_usdt <= _POSITION_EPSILON:
                position_size_usdt = max(float(config.position_size_usdt), 1.0)
            db_open_position = AutoTradePosition(
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                account_id=config.account_id,
                symbol=execution_symbol,
                side=live_side,
                status=POSITION_OPEN,
                entry_price=resolved_entry_price,
                quantity=live_contracts,
                position_size_usdt=position_size_usdt,
                leverage=resolved_leverage,
                tp_price=live_take_profit or resolved_entry_price,
                sl_price=live_stop_loss or resolved_entry_price,
                entry_confidence_pct=0.0,
                opened_at=_utc_now(),
                closed_at=None,
                close_reason=None,
                close_price=None,
                open_order_id=None,
                close_order_id=None,
                open_history_id=history_id,
                close_history_id=None,
                raw_open_order=(
                    exchange_position.raw
                    if isinstance(exchange_position.raw, dict)
                    else {}
                ),
                raw_close_order={},
                state=PositionState.OPEN.value,
                original_quantity=Decimal(str(live_contracts)),
                current_quantity=Decimal(str(live_contracts)),
            )
            session.add(db_open_position)
            await session.flush()
            if emit_events:
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=history_id,
                    position_id=db_open_position.id,
                    event_type="position_synced_open_from_exchange",
                    level=EVENT_LEVEL_INFO,
                    message="Created local open position snapshot from exchange.",
                    payload={
                        "symbol": db_open_position.symbol,
                        "side": db_open_position.side,
                        "quantity": db_open_position.quantity,
                    },
                    commit=False,
                )
            return db_open_position, True, exchange_position

        changed = False
        if db_open_position.symbol != execution_symbol:
            db_open_position.symbol = execution_symbol
            changed = True
        if db_open_position.account_id != config.account_id:
            db_open_position.account_id = config.account_id
            changed = True
        if db_open_position.config_id != config.id:
            db_open_position.config_id = config.id
            changed = True
        if db_open_position.profile_id != config.profile_id:
            db_open_position.profile_id = config.profile_id
            changed = True
        if db_open_position.side != live_side:
            db_open_position.side = live_side
            changed = True
        if db_open_position.state not in _OPEN_POSITION_STATE_NAMES:
            db_open_position.state = PositionState.OPEN.value
            changed = True
        if not math.isclose(
            float(db_open_position.quantity),
            live_contracts,
            rel_tol=0.0,
            abs_tol=_POSITION_EPSILON,
        ):
            db_open_position.quantity = live_contracts
            changed = True
        target_current_quantity = Decimal(str(live_contracts))
        if db_open_position.current_quantity != target_current_quantity:
            db_open_position.current_quantity = target_current_quantity
            changed = True
        if db_open_position.original_quantity is None:
            db_open_position.original_quantity = target_current_quantity
            changed = True
        if resolved_entry_price is not None and not math.isclose(
            float(db_open_position.entry_price),
            resolved_entry_price,
            rel_tol=0.0,
            abs_tol=_POSITION_EPSILON,
        ):
            db_open_position.entry_price = resolved_entry_price
            changed = True
        if db_open_position.leverage != resolved_leverage:
            db_open_position.leverage = resolved_leverage
            changed = True
        target_tp_price = live_take_profit or db_open_position.tp_price
        if target_tp_price <= _POSITION_EPSILON:
            target_tp_price = db_open_position.entry_price
        if not math.isclose(
            float(db_open_position.tp_price),
            float(target_tp_price),
            rel_tol=0.0,
            abs_tol=_POSITION_EPSILON,
        ):
            db_open_position.tp_price = float(target_tp_price)
            changed = True
        target_sl_price = live_stop_loss or db_open_position.sl_price
        if target_sl_price <= _POSITION_EPSILON:
            target_sl_price = db_open_position.entry_price
        if not math.isclose(
            float(db_open_position.sl_price),
            float(target_sl_price),
            rel_tol=0.0,
            abs_tol=_POSITION_EPSILON,
        ):
            db_open_position.sl_price = float(target_sl_price)
            changed = True
        exchange_raw = exchange_position.raw if isinstance(exchange_position.raw, dict) else {}
        if exchange_raw and db_open_position.raw_open_order != exchange_raw:
            db_open_position.raw_open_order = exchange_raw
            changed = True
        if duplicate_open_rows:
            now = _utc_now()
            for duplicate in duplicate_open_rows:
                duplicate.status = POSITION_CLOSED
                duplicate.state = PositionState.CLOSED.value
                duplicate.closed_at = now
                duplicate.close_reason = "deduplicated_on_exchange_sync"
                duplicate.close_history_id = history_id
                duplicate.close_price = None
                duplicate.current_quantity = Decimal("0")
                self._normalize_legacy_closed_position(duplicate)
            changed = True
        return db_open_position, changed, exchange_position

    @staticmethod
    def _normalize_legacy_closed_position(position: AutoTradePosition) -> bool:
        if position.status != POSITION_CLOSED:
            return False
        if position.close_reason != "already_closed_on_exchange":
            return False
        if position.close_order_id is not None:
            return False
        if position.close_price is None:
            return False
        if not math.isclose(
            float(position.close_price),
            float(position.entry_price),
            rel_tol=0.0,
            abs_tol=_POSITION_EPSILON,
        ):
            return False
        # Legacy rows used entry_price as synthetic close price for unknown exchange close.
        position.close_price = None
        return True

    async def _get_latest_open_position(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int | None = None,
    ) -> AutoTradePosition | None:
        stmt = select(AutoTradePosition).where(
            AutoTradePosition.user_id == user_id,
            AutoTradePosition.status == POSITION_OPEN,
        )
        if account_id is not None:
            stmt = stmt.where(AutoTradePosition.account_id == account_id)
        return cast(
            AutoTradePosition | None,
            await session.scalar(
                stmt.order_by(desc(AutoTradePosition.id)).limit(1)
            ),
        )

    async def _confirm_position_closed(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
    ) -> tuple[bool, NormalizedFuturesPosition | None]:
        latest: NormalizedFuturesPosition | None = None
        for attempt in range(1, _POSITION_CLOSE_CONFIRM_ATTEMPTS + 1):
            latest = await self._trading.fetch_futures_position(
                session=session,
                user_id=user_id,
                account_id=account_id,
                symbol=symbol,
            )
            if self._is_position_closed_on_exchange(latest):
                return True, latest
            if attempt < _POSITION_CLOSE_CONFIRM_ATTEMPTS:
                await asyncio.sleep(_POSITION_CLOSE_CONFIRM_DELAY_SECONDS * attempt)
        return False, latest

    def _calculate_tp_sl(
        self,
        *,
        signal: ParsedAutoTradeSignal,
        config: AutoTradeConfig,
    ) -> tuple[float, float]:
        return self._calculate_tp_sl_for_entry_price(
            entry_price=signal.price_current,
            trend=signal.trend,
            config=config,
        )

    def _calculate_tp_sl_for_entry_price(
        self,
        *,
        entry_price: float,
        trend: str,
        config: AutoTradeConfig,
    ) -> tuple[float, float]:
        entry = float(entry_price)
        tp_pct = config.tp_pct / 100.0
        sl_pct = config.sl_pct / 100.0
        if trend == TREND_LONG:
            tp = entry * (1 + tp_pct)
            sl = entry * (1 - sl_pct)
        else:
            tp = entry * (1 - tp_pct)
            sl = entry * (1 + sl_pct)
        return float(tp), float(sl)

    async def _get_or_create_signal_state(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
    ) -> AutoTradeSignalState:
        stmt: Select[tuple[AutoTradeSignalState]] = select(AutoTradeSignalState).where(
            AutoTradeSignalState.config_id == config.id
        )
        stmt = self._with_for_update(session=session, stmt=stmt)
        row = cast(AutoTradeSignalState | None, await session.scalar(stmt))
        if row is not None:
            return row

        row = AutoTradeSignalState(
            user_id=config.user_id,
            config_id=config.id,
            last_processed_history_id=0,
            last_trend=None,
            opposite_streak=0,
            last_signal_confidence_pct=None,
            last_signal_at=None,
        )
        session.add(row)
        await session.flush()
        return row

    async def _get_config_for_scope(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int | None,
        fail_on_ambiguous: bool,
        lock_for_update: bool,
    ) -> AutoTradeConfig | None:
        stmt: Select[tuple[AutoTradeConfig]] = (
            select(AutoTradeConfig)
            .where(AutoTradeConfig.user_id == user_id)
            .order_by(AutoTradeConfig.id.asc())
        )
        if account_id is not None:
            stmt = stmt.where(AutoTradeConfig.account_id == account_id)
            if lock_for_update:
                stmt = self._with_for_update(session=session, stmt=stmt)
            return cast(AutoTradeConfig | None, await session.scalar(stmt.limit(1)))

        scoped_stmt = self._with_for_update(session=session, stmt=stmt) if lock_for_update else stmt
        rows = list((await session.scalars(scoped_stmt.limit(2))).all())
        if not rows:
            return None
        if fail_on_ambiguous and len(rows) > 1:
            raise ValueError(
                "Multiple auto-trade configs found. Provide account_id to select config scope."
            )
        return rows[0]

    async def _get_config_for_update(
        self,
        *,
        session: AsyncSession,
        config_id: int,
    ) -> AutoTradeConfig | None:
        stmt: Select[tuple[AutoTradeConfig]] = select(AutoTradeConfig).where(
            AutoTradeConfig.id == config_id
        )
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            return cast(AutoTradeConfig | None, await session.scalar(stmt))

        row = cast(
            AutoTradeConfig | None,
            await session.scalar(stmt.with_for_update(skip_locked=True)),
        )
        if row is not None:
            return row

        exists = await session.scalar(
            select(AutoTradeConfig.id).where(AutoTradeConfig.id == config_id).limit(1)
        )
        if exists is not None:
            raise ExchangeServiceError(
                code="temporary_unavailable",
                message="Auto-trade config is currently locked by another worker.",
                retryable=True,
            )
        return None

    @staticmethod
    def _mark_completed(
        *,
        queue_item: AutoTradeSignalQueue,
        now: datetime,
        error: str | None,
    ) -> None:
        queue_item.status = QUEUE_COMPLETED
        queue_item.processed_at = now
        queue_item.locked_at = None
        queue_item.last_error = error

    def _mark_retry_or_dead(
        self,
        *,
        queue_item: AutoTradeSignalQueue,
        now: datetime,
        error: str,
        retryable: bool,
    ) -> bool:
        queue_item.last_error = error
        queue_item.locked_at = None
        if retryable and queue_item.attempt < queue_item.max_attempts:
            queue_item.status = QUEUE_PENDING
            delay_seconds = self._retry_interval_seconds * max(queue_item.attempt, 1)
            queue_item.next_retry_at = now + timedelta(seconds=delay_seconds)
            return True
        queue_item.status = QUEUE_DEAD
        queue_item.processed_at = now
        return False

    async def _emit_event(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        config_id: int | None,
        profile_id: int | None,
        history_id: int | None,
        position_id: int | None,
        event_type: str,
        level: str,
        message: str | None,
        payload: dict[str, Any],
        commit: bool,
    ) -> None:
        session.add(
            AutoTradeEvent(
                user_id=user_id,
                config_id=config_id,
                profile_id=profile_id,
                history_id=history_id,
                position_id=position_id,
                event_type=event_type,
                level=level,
                message=message,
                payload=payload,
            )
        )
        if commit:
            await session.commit()

    def _build_client_order_id(
        self, *, prefix: str, user_id: int, config_id: int, history_id: int
    ) -> str:
        value = f"{prefix}-{user_id}-{config_id}-{history_id}"
        return value[:64]

    async def _record_order_metadata(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        order_id: str | None,
        client_order_id: str | None,
        symbol: str,
        source: str,
        config_id: int | None,
        position_id: int | None,
        history_id: int | None,
    ) -> None:
        exchange_name = await session.scalar(
            select(ExchangeCredential.exchange_name).where(ExchangeCredential.id == account_id)
        )
        session.add(
            ExchangeOrderMetadata(
                user_id=user_id,
                account_id=account_id,
                exchange_name=str(exchange_name or "unknown"),
                symbol=symbol,
                exchange_order_id=str(order_id) if order_id else None,
                client_order_id=client_order_id,
                source=source,
                config_id=config_id,
                position_id=position_id,
                history_id=history_id,
            )
        )

    @staticmethod
    def _safe_chart_symbol(symbol: str) -> str:
        try:
            return to_chart_symbol(symbol)
        except ValueError:
            return symbol

    @staticmethod
    def _split_symbol(symbol: str) -> tuple[str, str] | None:
        if "/" not in symbol:
            return None
        left, right = symbol.split("/", 1)
        quote = right.split(":", 1)[0]
        base = left.strip().upper()
        quote_clean = quote.strip().upper()
        if not base or not quote_clean:
            return None
        return base, quote_clean

    @classmethod
    def _fee_to_quote(cls, *, trade: NormalizedTrade, fallback_symbol: str) -> float:
        if trade.fee_cost <= 0 or not trade.fee_currency:
            return 0.0
        parsed = cls._split_symbol(trade.symbol) or cls._split_symbol(fallback_symbol)
        if parsed is None:
            return 0.0
        base_asset, quote_asset = parsed
        fee_asset = trade.fee_currency.upper()
        if fee_asset == quote_asset:
            return float(trade.fee_cost)
        if fee_asset == base_asset:
            price = float(trade.price)
            return float(trade.fee_cost) * price if price > 0 else 0.0
        return 0.0

    @staticmethod
    def _trade_matches_order(trade: NormalizedTrade, order_ids: set[str]) -> bool:
        if trade.order_id and str(trade.order_id) in order_ids:
            return True
        raw = trade.raw
        if not isinstance(raw, dict):
            return False
        for key in ("orderLinkId", "clientOrderId", "orderId"):
            candidate = raw.get(key)
            if candidate and str(candidate) in order_ids:
                return True
        return False

    async def _fetch_position_fees_usdt(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        position: AutoTradePosition,
    ) -> float:
        order_ids = {
            str(order_id)
            for order_id in (position.open_order_id, position.close_order_id)
            if order_id
        }
        if not order_ids:
            return 0.0
        try:
            since = position.opened_at - timedelta(minutes=5)
            trades = await self._trading.fetch_futures_trades(
                session=session,
                user_id=user_id,
                account_id=position.account_id,
                symbol=position.symbol,
                since=since,
                limit=200,
            )
        except Exception:
            return 0.0
        fees_total = 0.0
        for trade in trades:
            if not self._trade_matches_order(trade, order_ids):
                continue
            fees_total += self._fee_to_quote(trade=trade, fallback_symbol=position.symbol)
        return fees_total

    async def _infer_closed_position_from_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        position: AutoTradePosition,
    ) -> dict[str, float] | None:
        if position.closed_at is None:
            return None
        try:
            opened_at = self._coerce_utc_datetime(position.opened_at)
            since = opened_at - timedelta(hours=12)
            trades = await self._trading.fetch_futures_trades(
                session=session,
                user_id=user_id,
                account_id=position.account_id,
                symbol=position.symbol,
                since=since,
                limit=500,
            )
        except Exception:
            return None
        exit_side: OrderSide = "sell" if position.side == TREND_LONG else "buy"
        candidates = [
            trade
            for trade in trades
            if trade.side == exit_side
            and trade.amount > _POSITION_EPSILON
            and trade.price > _POSITION_EPSILON
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: item.timestamp or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        target_qty = float(position.quantity)
        if target_qty <= _POSITION_EPSILON:
            return None
        remaining = target_qty
        matched_qty = 0.0
        weighted_cost = 0.0
        fee_total = 0.0
        realized_from_exchange = 0.0
        realized_hits = 0
        for trade in candidates:
            if remaining <= _POSITION_EPSILON:
                break
            take_qty = min(float(trade.amount), remaining)
            if take_qty <= _POSITION_EPSILON:
                continue
            matched_qty += take_qty
            weighted_cost += float(trade.price) * take_qty
            fee_total += self._fee_to_quote(trade=trade, fallback_symbol=position.symbol)
            closed_pnl = self._extract_trade_closed_pnl(trade)
            if closed_pnl is not None and math.isfinite(closed_pnl):
                realized_from_exchange += closed_pnl
                realized_hits += 1
            remaining -= take_qty
        if matched_qty <= _POSITION_EPSILON:
            return None
        close_price = weighted_cost / matched_qty
        realized = None
        if realized_hits > 0:
            realized = float(realized_from_exchange)
        else:
            directional = self._directional_pnl(
                side=position.side,
                entry_price=float(position.entry_price),
                mark_price=close_price,
                quantity=matched_qty,
            )
            if directional is not None:
                realized = float(directional) - fee_total
        if realized is None:
            return None
        return {"close_price": float(close_price), "realized_pnl_usdt": float(realized)}

    @staticmethod
    def _extract_trade_closed_pnl(trade: NormalizedTrade) -> float | None:
        raw = trade.raw
        if not isinstance(raw, dict):
            return None
        info = raw.get("info")
        sources: list[dict[str, Any]] = [raw]
        if isinstance(info, dict):
            sources.insert(0, info)
        for source in sources:
            for key in ("closedPnl", "closed_pnl", "execPnl", "exec_pnl"):
                value = source.get(key)
                if value is None:
                    continue
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed):
                    return parsed
        return None

    @staticmethod
    def _coerce_utc_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _positive_or_none(value: int | float | str | None) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed <= 0:
            return None
        return parsed

    @staticmethod
    def _ratio_percent(numerator: float | None, denominator: float) -> float | None:
        if numerator is None:
            return None
        if not math.isfinite(numerator):
            return None
        if denominator <= _POSITION_EPSILON or not math.isfinite(denominator):
            return None
        return float((numerator / denominator) * 100.0)

    @staticmethod
    def _directional_pnl(
        *,
        side: str,
        entry_price: float,
        mark_price: float | None,
        quantity: float,
    ) -> float | None:
        if mark_price is None:
            return None
        if side == TREND_LONG:
            return float((mark_price - entry_price) * quantity)
        return float((entry_price - mark_price) * quantity)

    def _with_for_update_skip_locked(
        self,
        *,
        session: AsyncSession,
        stmt: Select[tuple[AutoTradeSignalQueue]],
    ) -> Select[tuple[AutoTradeSignalQueue]]:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            return stmt
        return stmt.with_for_update(skip_locked=True)

    def _with_for_update(
        self,
        *,
        session: AsyncSession,
        stmt: Select[tuple[T]],
    ) -> Select[tuple[T]]:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            return stmt
        return stmt.with_for_update()
