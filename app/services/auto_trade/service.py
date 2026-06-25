import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, TypeVar, cast

from pydantic import ValidationError
from sqlalchemy import Select, case, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_event import AutoTradeEvent
from app.models.auto_trade_position import AutoTradePosition
from app.models.auto_trade_config_revision import AutoTradeConfigRevision
from app.models.auto_trade_risk_config import AutoTradeRiskConfig
from app.models.auto_trade_signal_queue import AutoTradeSignalQueue
from app.models.auto_trade_signal_state import AutoTradeSignalState
from app.models.exchange import EXCHANGE_MODE_DEMO, ExchangeCredential
from app.models.exchange_order_metadata import ExchangeOrderMetadata
from app.models.exchange_trade_ledger import ExchangeTradeLedger
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.strategy_promotion_event import StrategyPromotionEvent
from app.schemas.ai_overlay import AiOverlayConfig
from app.schemas.auto_trade import (
    AutoTradeClosedPositionInfo,
    AutoTradeCloseOpenPositionsResponse,
    AutoTradeClosePreview,
    AutoTradeClosePreviewItem,
    AutoTradeConfigRead,
    AutoTradeConfigUpsertRequest,
    AutoTradeFailedClosePositionInfo,
    PromotionGateCriterionRead,
    PromotionStatusRead,
)
from app.schemas.auto_trade import (
    AutoTradeRiskConfig as AutoTradeRiskConfigSchema,
)
from app.schemas.exchange_trading import NormalizedFuturesPosition, NormalizedTrade, OrderSide
from app.schemas.strategy_profile import StrategyProfileConfig
from app.services.auto_trade.ai_overlay import (
    AiOverlayEventType,
    build_overlay_payload,
    resolve_ai_trend,
    scale_atr_multiplier,
    scale_rsi_thresholds,
    shift_watcher_condition_threshold,
    should_block_entry,
)
from app.services.auto_trade.anomaly import AnomalyConfig, detect_anomalies
from app.services.auto_trade.health import (
    StrategyHealth,
    gross_realized_pnl,
    compute_strategy_health,
    prune_strategy_health_snapshots,
    record_health_snapshot,
)
from app.services.auto_trade.income_sync import sum_funding
from app.services.auto_trade.promotion import (
    InvalidPromotionError,
    LifecycleStage,
    PromotionDecision,
    PromotionTrigger,
    apply_transition,
    evaluate_promotion_gate,
)
from app.services.auto_trade.risk import (
    GuardDecision,
    RiskDecision,
    check_pre_trade,
    evaluate_kpi_guard,
)
from app.services.events.stream import publish_user_event, queue_user_event
from app.services.exchange.adapter import (
    ConditionalOrderResult,
    EntryOrderResult,
)
from app.services.exchange.adapter import (
    OrderSide as ExchangeOrderSide,
)
from app.services.exchange_credentials.service import ExchangeCredentialsService
from app.services.execution.errors import ExchangeServiceError
from app.services.execution.futures_pnl import RealizedBreakdown, compute_realized_breakdown
from app.services.execution.trading_service import TradingService
from app.services.personal_analysis.provider import AnalysisProviderError
from app.services.position.context import (
    PositionContext,
    SLHistoryEntry,
    TPLevel,
    WatcherConfig,
)
from app.services.position.context import (
    PositionSide as RuntimePositionSide,
)
from app.services.position.order_queue import OrderPriority, OrderTask
from app.services.position.state_machine import PositionState, TransitionTrigger
from app.services.sl_tp.kill_switch import KillSwitchSignal
from app.services.sl_tp.live_tracker import RealtimeSLAdjuster
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


class PromotionGateError(Exception):
    """Raised when a sandbox→live promotion is refused by the KPI Gate.

    Carries the :class:`PromotionDecision` so the API can surface exactly which
    criteria failed (actual vs threshold).
    """

    def __init__(self, decision: PromotionDecision) -> None:
        self.decision = decision
        failed = ", ".join(c.name for c in decision.failed)
        super().__init__(f"Promotion gate not satisfied: {failed}")


def _anomaly_config_from_risk(risk_cfg: AutoTradeRiskConfig) -> AnomalyConfig:
    """Build the detector config from a risk row; NULLs fall back to engine defaults."""
    kwargs: dict[str, Any] = {}
    if risk_cfg.anomaly_z_threshold is not None:
        kwargs["z_threshold"] = float(risk_cfg.anomaly_z_threshold)
    if risk_cfg.anomaly_window is not None:
        kwargs["window"] = int(risk_cfg.anomaly_window)
    return AnomalyConfig(**kwargs)


def _gate_criteria_payload(decision: PromotionDecision) -> list[dict[str, Any]]:
    """Serialize the KPI-Gate criteria (actual vs threshold) for an event payload."""
    return [
        {
            "name": c.name,
            "actual": c.actual,
            "threshold": c.threshold,
            "passed": c.passed,
        }
        for c in decision.criteria
    ]
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
_RECONCILER_INTERVAL_SECONDS = 60.0
# How often the REST reconcile polls open positions for closure. Independent of
# (and faster than) the hydrate cadence: the user-data WebSocket is unreliable
# on some real accounts (connects but delivers no fill events), so REST is the
# authoritative safety net for detecting SL/TP fills / closures within seconds
# instead of up to an hour (the old signal-time-only sync).
_POSITION_RECONCILE_INTERVAL_SECONDS = 15.0
# T7 (W5b): backoff before re-subscribing the in-position indicator watcher consumer
# after a transient failure (e.g. Redis blip). Keeps the consumer alive for the life
# of the process without a tight crash-loop.
_WATCHER_RESUBSCRIBE_DELAY_SECONDS = 5.0
logger = logging.getLogger(__name__)

T = TypeVar("T")
_SUPPORTED_AUTO_TRADE_FUTURES_EXCHANGES = {"bybit", "binance"}

# A1 (audit §2.5.3): venue maximum leverage for USDT-M perpetuals. A
# ``leverage_ceiling`` above the venue max passes the schema bound (<=125, the
# Binance max) yet is unattainable on the exchange, so the order would be
# rejected at placement time. We validate the ceiling against the actual venue
# at config-write time. Unknown venues fall back to the permissive default so we
# never *tighten* behaviour for an exchange we don't have a number for.
_EXCHANGE_MAX_LEVERAGE: dict[str, int] = {"binance": 125, "bybit": 100}
_DEFAULT_MAX_LEVERAGE = 125


def _exchange_max_leverage(exchange_name: str) -> int:
    return _EXCHANGE_MAX_LEVERAGE.get((exchange_name or "").strip().lower(), _DEFAULT_MAX_LEVERAGE)


def _assert_leverage_ceiling_within_exchange(exchange_name: str, leverage_ceiling: int) -> None:
    """Raise ``ValueError`` if ``leverage_ceiling`` exceeds the venue maximum."""
    max_leverage = _exchange_max_leverage(exchange_name)
    if leverage_ceiling > max_leverage:
        raise ValueError(
            f"leverage_ceiling {leverage_ceiling} exceeds the "
            f"{exchange_name or 'exchange'} maximum leverage of {max_leverage}."
        )


def _config_content_snapshot(
    config: AutoTradeConfig, risk_cfg: AutoTradeRiskConfig | None
) -> dict[str, Any]:
    """§7: the editable config-content fields (and the 1:1 risk row) that a
    revision captures and a rollback restores. Runtime state (``is_running``,
    ``lifecycle_stage``, ``risk_off_*``, timestamps) is intentionally excluded —
    it is not user-edited content and must not be rolled back.
    """
    return {
        "enabled": config.enabled,
        "profile_id": config.profile_id,
        "account_id": config.account_id,
        "position_size_usdt": config.position_size_usdt,
        "leverage": config.leverage,
        "min_confidence_pct": config.min_confidence_pct,
        "fast_close_confidence_pct": config.fast_close_confidence_pct,
        "confirm_reports_required": config.confirm_reports_required,
        "risk_mode": config.risk_mode,
        "sl_pct": config.sl_pct,
        "tp_pct": config.tp_pct,
        "strategy_profile": config.strategy_profile_json,
        "strategy_name": config.strategy_name,
        "attached_forecast_id": config.attached_forecast_id,
        "risk": (
            AutoTradeRiskConfigSchema.model_validate(risk_cfg).model_dump(mode="json")
            if risk_cfg is not None
            else None
        ),
    }


def _content_hash(snapshot: dict[str, Any]) -> str:
    """Stable sha256 of a content snapshot (canonical, key-sorted JSON)."""
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfirmationRequiredError(Exception):
    """Raised when a destructive operation needs explicit confirmation.

    The endpoint translates this into HTTP 412 Precondition Failed and
    serialises ``preview`` into the response body so the client can show
    the user exactly what would happen before they re-send with
    ``confirm=true``.
    """

    def __init__(self, preview: Any) -> None:
        super().__init__("Confirmation required for destructive operation.")
        self.preview = preview


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


def _overlay_active_phases_label(overlay: AiOverlayConfig) -> str:
    """Human-readable comma-separated list of enabled overlay phases."""
    flags = []
    if overlay.entry_side_lock_enabled:
        flags.append("entry_side_lock")
    if overlay.atr_scaling_enabled:
        flags.append("atr_scaling")
    if overlay.rsi_scaling_enabled:
        flags.append("rsi_scaling")
    return ",".join(flags) if flags else "none"


def _resolve_base_atr_multiplier(config: AutoTradeConfig) -> float:
    """Return the base ATR multiplier the overlay should scale.

    Mirrors the default used in ``_build_position_context``: read from the
    persisted strategy profile when present, else fall back to 2.0.
    """
    profile = _parse_strategy_profile(config.strategy_profile_json)
    if profile is None:
        return 2.0
    return float(profile.volatility_atr_multiplier)


# Re-export for inline annotation inside ``_process_without_open_position``
# without polluting the module-level ``Literal`` aliases used elsewhere.
PositionSideLiteral = Literal["long", "short"]


def _parse_strategy_profile(
    payload: dict[str, object] | None,
) -> StrategyProfileConfig | None:
    """Parse and validate the persisted strategy profile JSON.

    Returns None on missing payload or validation failure, but a validation
    failure is logged at ERROR (was previously silently swallowed) so the
    misconfiguration surfaces in application logs. Phase 3.15 prevents the
    most common silent-fail mode at strategy save time.
    """
    if payload is None:
        return None
    try:
        return StrategyProfileConfig.model_validate(payload)
    except ValidationError as exc:
        logger.error(
            "strategy_profile validation failed; per-level TP/SL config will be "
            "dropped at runtime. payload_keys=%s errors=%s",
            sorted(payload.keys()) if isinstance(payload, dict) else None,
            exc.errors(),
        )
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
        volatility_atr_multiplier_override: float | None = None,
        rsi_condition_shift: int = 0,
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

        # W4 / Phase 2: AI Trend Overlay — runtime ATR multiplier override.
        # Applied *after* the base value has been resolved from the strategy
        # profile, so the overlay always scales the user-configured anchor.
        if volatility_atr_multiplier_override is not None:
            volatility_atr_multiplier = float(volatility_atr_multiplier_override)

        if strategy_profile is not None:
            active_watchers = []
            for watcher in strategy_profile.watchers:
                # W4 / Phase 3: shift threshold-based conditions for RSI watchers
                # by the overlay-supplied amount. The transformer is a no-op
                # when ``rsi_condition_shift == 0`` so this stays free for the
                # default path.
                effective_condition = watcher.condition
                if rsi_condition_shift != 0 and watcher.indicator.upper() == "RSI":
                    effective_condition = shift_watcher_condition_threshold(
                        watcher.condition, rsi_condition_shift
                    )
                active_watchers.append(
                    WatcherConfig(
                        indicator=watcher.indicator,
                        params=dict(watcher.params),
                        condition=effective_condition,
                        action=watcher.action,
                        action_params=dict(watcher.action_params),
                        is_active=watcher.is_active,
                    )
                )
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
                    kill_switch_handler=self._runtime_kill_switch_close,
                )
                _WS_MANAGER_REGISTRY[position.account_id] = manager
                await manager.start()
            elif not manager.is_connected() and not manager.is_reconnecting():
                # Bug W safety-net: the cached manager's user-data stream is
                # down (e.g. reconnect attempts were exhausted, or it never
                # recovered). Restart it so the 60s hydrate loop revives a dead
                # account stream instead of silently tracking positions onto a
                # connection that delivers no fills.
                logger.warning(
                    "[%s] WS manager not connected; restarting user-data stream.",
                    position.account_id,
                )
                await manager.start()

            # Populate the kill-switch config onto the context before tracking so
            # the realtime tick (on_tick) can evaluate the spike detector. Single
            # funnel for both the entry and hydration paths.
            await self._apply_kill_switch_config(session=session, position=position)
            manager.track_position(position)

            # When tracking a position that's already OPEN (e.g. on hydration
            # after restart), proactively start the realtime pipeline. The
            # entry-fill code path normally handles this, but it never fires
            # for hydrated positions because the entry has already filled.
            if (
                position.state == PositionState.OPEN
                and RealtimeSLAdjuster.needs_realtime_monitoring(position)
            ):
                asyncio.create_task(
                    manager._ensure_realtime_sl_pipeline(position),  # noqa: SLF001
                    name=f"sl_pipeline_init:{position.position_id}",
                )

    async def _audit_emit_handler(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist a low-level subsystem audit emit as an ``auto_trade_event`` row.

        Looks up the referenced position to populate user_id / config_id /
        profile_id. Failures are logged but never raised — audit must never
        break safety-critical code paths.
        """
        position_id_raw = payload.get("position_id")
        if position_id_raw is None:
            logger.debug(
                "_audit_emit_handler: %s without position_id, skipping persist",
                event_type,
            )
            return

        try:
            position_id_int = int(str(position_id_raw))
        except (TypeError, ValueError):
            logger.debug(
                "_audit_emit_handler: %s with non-numeric position_id=%r",
                event_type,
                position_id_raw,
            )
            return

        try:
            async with AsyncSessionFactory() as session:
                position = await session.get(AutoTradePosition, position_id_int)
                if position is None:
                    return
                event_level = self._severity_for_audit_event_type(event_type)
                # ``message`` is consumed by the Quick Log UI. Subsystems
                # that supply a human-readable summary include it in the
                # payload under the ``message`` key; everything else is
                # left as None to preserve the JSON-only behaviour and
                # not regress on existing audit consumers.
                raw_message = payload.get("message")
                message = (
                    str(raw_message) if isinstance(raw_message, str) and raw_message else None
                )
                session.add(
                    AutoTradeEvent(
                        user_id=int(position.user_id),
                        config_id=position.config_id,
                        profile_id=position.profile_id,
                        history_id=position.open_history_id,
                        position_id=position.id,
                        event_type=event_type,
                        level=event_level,
                        message=message,
                        payload=dict(payload),
                    )
                )
                await session.commit()
        except Exception:
            logger.exception(
                "_audit_emit_handler: failed to persist event_type=%s position_id=%s",
                event_type,
                position_id_raw,
            )

    @staticmethod
    def _severity_for_audit_event_type(event_type: str) -> str:
        if event_type in {
            "tp_fill_unmatched",
            "sl_adjustment_failed",
            "order_task_fatal_error",
            "strategy_profile_validation_failed",
            "cancel_remaining_orders_quiesce_timeout",
        }:
            return EVENT_LEVEL_ERROR
        if event_type in {
            "sl_adjustment_skipped",
            "sl_adjustment_skipped_position_already_flat",
            "sl_adjustment_skipped_would_trigger_immediately_vs_mark",
            "sl_adjustment_clamped_to_safe_distance",
            "multi_tp_inferred_from_position_update",
            "multi_tp_duplicate_dispatch_ignored",
            "emergency_close_skipped_position_flat",
        }:
            return EVENT_LEVEL_WARNING
        return EVENT_LEVEL_INFO

    async def hydrate_active_positions(self) -> int:
        """Re-track every OPEN auto-trade position with the WS manager.

        Called from FastAPI lifespan startup and (cheaply) every 60s by the
        reconciler loop. Idempotent: tracking the same position twice is a
        no-op, and ``_ensure_realtime_sl_pipeline`` short-circuits when the
        symbol is already subscribed.

        Returns the number of positions hydrated.
        """
        from app.services.position.context import PositionContext as _PositionContext

        hydrated = 0
        try:
            async with AsyncSessionFactory() as session:
                rows = await session.execute(
                    select(AutoTradePosition, ExchangeCredential.exchange_name)
                    .join(
                        ExchangeCredential,
                        ExchangeCredential.id == AutoTradePosition.account_id,
                    )
                    .where(
                        AutoTradePosition.state.in_(
                            [
                                PositionState.PENDING.value,
                                PositionState.ENTERING.value,
                                PositionState.OPEN.value,
                                PositionState.ADJUSTING.value,
                                PositionState.CLOSING.value,
                                PositionState.RECONNECTING.value,
                                PositionState.ERROR_RECOVERY.value,
                            ]
                        )
                    )
                )
                for position_row, exchange_name in rows.all():
                    payload = {
                        column.name: getattr(position_row, column.name)
                        for column in AutoTradePosition.__table__.columns
                    }
                    payload["position_id"] = str(position_row.id)
                    payload["exchange"] = str(exchange_name or "")
                    try:
                        ctx = _PositionContext.from_db_row(payload)
                    except Exception:
                        logger.exception(
                            "hydrate_active_positions: failed to build context "
                            "for position_id=%s",
                            position_row.id,
                        )
                        continue

                    try:
                        await self._ensure_ws_manager_tracked(
                            session=session,
                            position=ctx,
                        )
                        hydrated += 1
                    except Exception:
                        logger.exception(
                            "hydrate_active_positions: failed to track "
                            "position_id=%s",
                            ctx.position_id,
                        )
        except Exception:
            logger.exception("hydrate_active_positions: top-level failure")
            return hydrated

        if hydrated:
            logger.info("hydrate_active_positions: hydrated=%d", hydrated)
        return hydrated

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
                        # Initial SL closes the entire live position at
                        # trigger time. After multi-TP partial fills the SL
                        # quantity stays in sync automatically — the engine
                        # only re-issues the SL when the trigger price moves
                        # (sl_lock_pct / move_sl_to).
                        "close_position": True,
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
        config_id: int | None = None,
        fail_on_ambiguous: bool = False,
    ) -> AutoTradeConfig | None:
        if config_id is not None:
            # Direct lookup by primary key; the W7 multi-strategy UI needs
            # this to disambiguate two configs sharing an account.
            row = await session.scalar(
                select(AutoTradeConfig).where(
                    AutoTradeConfig.user_id == user_id,
                    AutoTradeConfig.id == config_id,
                )
            )
            return cast(AutoTradeConfig | None, row)
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

        # A1: validate the risk leverage ceiling against the venue max *before*
        # any mutation, so an invalid ceiling never leaves a half-created config.
        if payload.risk is not None and payload.risk.leverage_ceiling is not None:
            _assert_leverage_ceiling_within_exchange(
                account.exchange_name, payload.risk.leverage_ceiling
            )

        now = _utc_now()
        stmt: Select[tuple[AutoTradeConfig]] = select(AutoTradeConfig).where(
            AutoTradeConfig.user_id == user_id,
            AutoTradeConfig.account_id == payload.account_id,
        )
        stmt = self._with_for_update(session=session, stmt=stmt)
        row = cast(AutoTradeConfig | None, await session.scalar(stmt))

        if row is None:
            # W7: surface a soft warning when the same profile is already
            # used by another strategy of this user. Two configs sharing one
            # profile produce identical signals → doubled exposure across
            # sub-accounts. Legitimate (e.g. mirror Binance to Bybit), but
            # the user should know.
            other_config_ids = list(
                (
                    await session.scalars(
                        select(AutoTradeConfig.id).where(
                            AutoTradeConfig.user_id == user_id,
                            AutoTradeConfig.profile_id == payload.profile_id,
                        )
                    )
                ).all()
            )
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
                strategy_name=payload.strategy_name,
                attached_forecast_id=payload.attached_forecast_id,
                last_started_at=None,
                last_stopped_at=now if not payload.enabled else None,
                # P4-4: new strategies start in sandbox — they validate on a demo
                # account and must clear the KPI gate (step-up) before going live.
                lifecycle_stage=LifecycleStage.SANDBOX.value,
                sandbox_entered_at=now,
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
                    "strategy_name": row.strategy_name,
                },
                commit=True,
            )
            if other_config_ids:
                await self._emit_event(
                    session=session,
                    user_id=user_id,
                    config_id=row.id,
                    profile_id=row.profile_id,
                    history_id=None,
                    position_id=None,
                    event_type="config_shares_profile_with",
                    level=EVENT_LEVEL_INFO,
                    message=(
                        "Profile is already used by another strategy of this user — "
                        "the new strategy will receive the same signals."
                    ),
                    payload={"other_config_ids": other_config_ids},
                    commit=True,
                )
            await self._apply_risk_config(session=session, config_id=row.id, risk=payload.risk)
            await self._record_config_revision(session=session, config=row, actor="user")
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
        if "strategy_name" in payload.model_fields_set:
            row.strategy_name = payload.strategy_name
        if "attached_forecast_id" in payload.model_fields_set:
            row.attached_forecast_id = payload.attached_forecast_id
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
        await self._apply_risk_config(session=session, config_id=row.id, risk=payload.risk)
        await self._record_config_revision(session=session, config=row, actor="user")
        return row

    async def get_risk_config(
        self,
        *,
        session: AsyncSession,
        config_id: int,
    ) -> AutoTradeRiskConfig | None:
        """Fetch the 1:1 Pre-Trade Risk row for a config, or ``None`` if unset."""
        return await session.get(AutoTradeRiskConfig, config_id)

    async def _record_config_revision(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        actor: str | None = None,
    ) -> None:
        """§7: append an immutable revision of the config's content, if changed.

        Hash-deduped: re-saving identical content (same ``content_hash`` as the
        latest revision) records nothing, so no-op updates don't inflate history.
        """
        risk_cfg = await self.get_risk_config(session=session, config_id=config.id)
        snapshot = _config_content_snapshot(config, risk_cfg)
        content_hash = _content_hash(snapshot)
        latest = await session.scalar(
            select(AutoTradeConfigRevision)
            .where(AutoTradeConfigRevision.config_id == config.id)
            .order_by(AutoTradeConfigRevision.revision_number.desc())
            .limit(1)
        )
        if latest is not None and latest.content_hash == content_hash:
            return
        next_number = (latest.revision_number + 1) if latest is not None else 1
        session.add(
            AutoTradeConfigRevision(
                config_id=config.id,
                revision_number=next_number,
                content_hash=content_hash,
                snapshot_json=snapshot,
                actor=actor,
            )
        )
        await session.commit()

    async def rollback_config(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        config_id: int,
        revision_id: int,
    ) -> AutoTradeConfig:
        """§7: restore a config to a prior revision's content.

        Re-applies the revision's snapshot through :meth:`upsert_config`, so the
        same validation runs and the rollback itself is recorded as a *new*
        revision (history stays append-only — nothing is rewritten). Runtime
        state (lifecycle stage, running flag, risk-off latch) is untouched.
        Raises ``LookupError`` if the config is not the caller's or the revision
        does not belong to it.
        """
        config = await session.get(AutoTradeConfig, config_id)
        if config is None or config.user_id != user_id:
            raise LookupError("Auto-trade config not found.")
        revision = await session.get(AutoTradeConfigRevision, revision_id)
        if revision is None or revision.config_id != config_id:
            raise LookupError("Config revision not found for this strategy.")
        snap = revision.snapshot_json
        request = AutoTradeConfigUpsertRequest(
            enabled=bool(snap["enabled"]),
            profile_id=int(cast(int, snap["profile_id"])),
            account_id=int(cast(int, snap["account_id"])),
            position_size_usdt=float(cast(float, snap["position_size_usdt"])),
            leverage=int(cast(int, snap["leverage"])),
            min_confidence_pct=float(cast(float, snap["min_confidence_pct"])),
            fast_close_confidence_pct=float(cast(float, snap["fast_close_confidence_pct"])),
            confirm_reports_required=int(cast(int, snap["confirm_reports_required"])),
            risk_mode=cast(Any, snap["risk_mode"]),
            sl_pct=float(cast(float, snap["sl_pct"])),
            tp_pct=float(cast(float, snap["tp_pct"])),
            strategy_profile=cast(Any, snap["strategy_profile"]),
            strategy_name=cast(Any, snap["strategy_name"]),
            attached_forecast_id=cast(Any, snap["attached_forecast_id"]),
            risk=cast(Any, snap["risk"]),
        )
        return await self.upsert_config(session=session, user_id=user_id, payload=request)

    async def evaluate_pre_trade_risk(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        signal: ParsedAutoTradeSignal,
        execution_symbol: str,
    ) -> RiskDecision:
        """Run the Pre-Trade Risk Engine for one prospective entry (W8).

        Resolves the config's risk row and computes the daily-loss inputs lazily
        (no exchange call unless that rule is configured *and* could fire), then
        delegates to the pure :func:`check_pre_trade`. A missing/disabled risk
        config returns ``allow`` (fail-safe). Shared by the auto-trade open path
        and the manual-order precheck so both honour the same envelope.
        """
        risk_cfg = await self.get_risk_config(session=session, config_id=config.id)
        today_realized_pnl_usdt = 0.0
        account_balance_usdt: float | None = None
        if risk_cfg is not None and risk_cfg.enabled:
            if (
                risk_cfg.daily_loss_limit_usdt is not None
                or risk_cfg.daily_loss_limit_pct is not None
            ):
                today_realized_pnl_usdt = await self._today_realized_pnl_usdt(
                    session=session, config_id=config.id
                )
            # Only pay for the (exchange) balance fetch when the percent rule can
            # actually fire — i.e. there is a realized loss today.
            if risk_cfg.daily_loss_limit_pct is not None and today_realized_pnl_usdt < 0:
                account_balance_usdt = await self._safe_subaccount_usdt_balance(
                    session=session, user_id=config.user_id, account_id=config.account_id
                )
        return await check_pre_trade(
            session=session,
            config=config,
            risk_cfg=risk_cfg,
            signal=signal,
            execution_symbol=execution_symbol,
            today_realized_pnl_usdt=today_realized_pnl_usdt,
            account_balance_usdt=account_balance_usdt,
        )

    async def precheck_manual_order(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        side: str,
        price: float,
    ) -> RiskDecision:
        """Apply the account's pre-trade risk envelope to a MANUAL order (§3).

        Manual live execution previously bypassed the supervisor. Here a manual
        OPEN order on an account that has an auto-trade config is run through the
        same :func:`check_pre_trade` engine, so the account's configured limits
        govern manual and automated entries alike.

        Fail-safe / non-breaking by construction:
        - No auto-trade config for ``(user, account)`` ⇒ ``allow`` (manual trading
          on un-configured accounts keeps today's behaviour).
        - No / disabled risk row ⇒ ``allow`` (the engine's own no-op).
        - Only opening orders are gated: a reducing order (spot ``sell`` / SHORT)
          is de-risking and is never blocked.

        A blocked decision records a ``risk_blocked`` audit event (``source:
        manual_order``) so the manual block is auditable.
        """
        # Only opening orders are gated — a reducing/closing order must never block.
        if side.strip().upper() != TREND_LONG:
            return RiskDecision.allow()
        config = await session.scalar(
            select(AutoTradeConfig).where(
                AutoTradeConfig.user_id == user_id,
                AutoTradeConfig.account_id == account_id,
            )
        )
        if config is None:
            return RiskDecision.allow()
        try:
            execution_symbol = to_linear_perp_symbol(symbol)
        except ValueError:
            execution_symbol = symbol
        signal = ParsedAutoTradeSignal(
            schema_version="1.0.0",
            symbol=symbol,
            trend="LONG",  # gated path is opening-only (guarded above)
            confidence_pct=100.0,
            price_current=float(price) if price else 0.0,
            generated_at=_utc_now(),
        )
        decision = await self.evaluate_pre_trade_risk(
            session=session,
            config=config,
            signal=signal,
            execution_symbol=execution_symbol,
        )
        if not decision.allowed:
            await self._emit_event(
                session=session,
                user_id=user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=None,
                position_id=None,
                event_type="risk_blocked",
                level=EVENT_LEVEL_WARNING,
                message=decision.reason or "Manual order blocked by the pre-trade risk engine.",
                payload={"rule": decision.rule, "source": "manual_order", **decision.payload},
                commit=True,
            )
        return decision

    async def _today_realized_pnl_usdt(
        self, *, session: AsyncSession, config_id: int, now: datetime | None = None
    ) -> float:
        """Net realized PnL booked by THIS strategy today (UTC) — **pure DB**.

        Prefers the synced ledger: ``net = Σ realized_pnl − commission + funding``
        over fills with ``traded_at >= UTC midnight`` for this config, funding from
        the income ledger for the same account+symbol. Every source is local, so
        the daily-loss gate makes **no exchange call** on the trading hot path
        (the previous per-position-snapshot version issued K live round-trips per
        signal — see git history). Falls back to a single price-based SQL
        aggregate over positions closed today when the config has no synced fills.

        Scoped to ``config_id`` so this numerator matches the per-account balance
        the percent daily-loss rule divides by.
        """
        day_start = (now or _utc_now()).replace(hour=0, minute=0, second=0, microsecond=0)
        fills = list(
            (
                await session.scalars(
                    select(ExchangeTradeLedger)
                    .where(
                        ExchangeTradeLedger.auto_trade_config_id == config_id,
                        ExchangeTradeLedger.traded_at >= day_start,
                    )
                    .order_by(ExchangeTradeLedger.traded_at)
                )
            ).all()
        )
        if fills:
            symbol = fills[0].symbol
            account_id = fills[0].account_id
            funding = await sum_funding(
                session=session, account_id=account_id, symbol=symbol, start=day_start
            )
            breakdown = compute_realized_breakdown(symbol=symbol, trades=fills, funding=funding)
            return breakdown.net_realized
        return await self._today_realized_pnl_legacy(
            session=session, config_id=config_id, day_start=day_start
        )

    async def _today_realized_pnl_legacy(
        self, *, session: AsyncSession, config_id: int, day_start: datetime
    ) -> float:
        """Fallback numerator: gross price PnL over positions closed today, from
        stored entry/close prices (no ledger fills available yet, no exchange)."""
        gross_pnl = case(
            (
                AutoTradePosition.side == TREND_LONG,
                (AutoTradePosition.close_price - AutoTradePosition.entry_price)
                * AutoTradePosition.quantity,
            ),
            (
                AutoTradePosition.side == TREND_SHORT,
                (AutoTradePosition.entry_price - AutoTradePosition.close_price)
                * AutoTradePosition.quantity,
            ),
            else_=0.0,
        )
        total = await session.scalar(
            select(func.coalesce(func.sum(gross_pnl), 0.0)).where(
                AutoTradePosition.config_id == config_id,
                AutoTradePosition.status == POSITION_CLOSED,
                AutoTradePosition.close_price.is_not(None),
                AutoTradePosition.closed_at >= day_start,
            )
        )
        return float(total or 0.0)

    async def _config_realized_net_usdt(
        self, *, session: AsyncSession, config_id: int
    ) -> float:
        """All-time **net** realized PnL for a strategy — pure DB, exchange-accurate.

        Model: one sub-account per strategy (the W7 design), so **every** fill on
        the strategy's account is attributed to the strategy — auto *and* manual.
        We therefore aggregate the synced ledger by **account**, not by
        ``auto_trade_config_id``: ``net = Σ realized_pnl − commission + funding``
        over all the account's fills (grouped by symbol). This makes the
        strategy/portfolio realized equal the per-account PnL card (they were
        diverging because manually-placed fills are tagged ``external`` and were
        excluded from the config-scoped sum).

        Makes **no exchange call**. Falls back to a price-based SQL aggregate over
        the config's closed positions when no fills are synced yet.
        """
        config = await session.get(AutoTradeConfig, config_id)
        if config is None:
            return 0.0
        fills = list(
            (
                await session.scalars(
                    select(ExchangeTradeLedger)
                    .where(ExchangeTradeLedger.account_id == config.account_id)
                    .order_by(ExchangeTradeLedger.traded_at)
                )
            ).all()
        )
        if fills:
            by_symbol: dict[str, list[ExchangeTradeLedger]] = {}
            for fill in fills:
                by_symbol.setdefault(fill.symbol, []).append(fill)
            net = 0.0
            for symbol, symbol_fills in by_symbol.items():
                funding = await sum_funding(
                    session=session,
                    account_id=symbol_fills[0].account_id,
                    symbol=symbol,
                )
                net += compute_realized_breakdown(
                    symbol=symbol, trades=symbol_fills, funding=funding
                ).net_realized
            return net
        return await self._config_realized_pnl_legacy(
            session=session, config_id=config_id
        )

    async def _config_realized_pnl_legacy(
        self, *, session: AsyncSession, config_id: int
    ) -> float:
        """Fallback: gross price PnL over ALL closed positions for the config, from
        stored entry/close prices (no ledger fills yet, no exchange)."""
        gross_pnl = case(
            (
                AutoTradePosition.side == TREND_LONG,
                (AutoTradePosition.close_price - AutoTradePosition.entry_price)
                * AutoTradePosition.quantity,
            ),
            (
                AutoTradePosition.side == TREND_SHORT,
                (AutoTradePosition.entry_price - AutoTradePosition.close_price)
                * AutoTradePosition.quantity,
            ),
            else_=0.0,
        )
        total = await session.scalar(
            select(func.coalesce(func.sum(gross_pnl), 0.0)).where(
                AutoTradePosition.config_id == config_id,
                AutoTradePosition.status == POSITION_CLOSED,
                AutoTradePosition.close_price.is_not(None),
            )
        )
        return float(total or 0.0)

    async def _safe_subaccount_usdt_balance(
        self, *, session: AsyncSession, user_id: int, account_id: int
    ) -> float | None:
        """Total USDT balance for a sub-account, or ``None`` if the call fails.

        Fail-open by design: a flaky exchange must never *block* a trade via the
        percent daily-loss rule (SPEC §6.3). The caller treats ``None`` as
        'balance unavailable' and skips the pct check with a warning.

        Only *expected* exchange/network failures fail open (and are logged so
        they're observable). An unexpected exception (a code bug) propagates so
        the signal dead-letters rather than silently disabling the loss limit
        (review I3) — fail-closed opens no trade.
        """
        try:
            snapshot = await self._trading.get_spot_balances(
                session=session, user_id=user_id, account_id=account_id
            )
        except (ExchangeServiceError, TimeoutError, OSError) as exc:
            logger.warning(
                "Daily-loss pct: balance fetch failed for account %s (%s) — failing open.",
                account_id,
                type(exc).__name__,
                exc_info=exc,
            )
            return None
        total = 0.0
        for item in snapshot.balances:
            if str(getattr(item, "asset", "")).upper() == "USDT":
                total += float(getattr(item, "total", 0.0) or 0.0)
        return total

    async def _apply_risk_config(
        self,
        *,
        session: AsyncSession,
        config_id: int,
        risk: AutoTradeRiskConfigSchema | None,
        commit: bool = True,
    ) -> None:
        """Create or wholesale-replace the 1:1 risk row from an upsert payload.

        ``risk is None`` is a deliberate no-op: omitting the block on update
        preserves any existing limits, and on insert creates no row at all —
        the engine treats a missing row as "every limit off" (fail-safe).
        When present, the payload is the full desired state, so every column
        is overwritten (unset limits become ``NULL``).

        ``commit=False`` lets the bulk apply-all path stage every config's row in
        one transaction so a leverage rejection on any config aborts the batch.
        """
        if risk is None:
            return
        # A1: authoritative leverage-ceiling check — covers every writer of the
        # risk row (single upsert *and* the bulk apply-all path) by resolving the
        # config's own venue, not the upsert payload's.
        if risk.leverage_ceiling is not None:
            config = await session.get(AutoTradeConfig, config_id)
            if config is not None:
                exchange_name = await self._resolve_account_exchange_name(
                    session=session, account_id=config.account_id
                )
                _assert_leverage_ceiling_within_exchange(exchange_name, risk.leverage_ceiling)
        row = await session.get(AutoTradeRiskConfig, config_id)
        if row is None:
            row = AutoTradeRiskConfig(config_id=config_id)
            session.add(row)
        row.enabled = bool(risk.enabled)
        row.daily_loss_limit_usdt = risk.daily_loss_limit_usdt
        row.daily_loss_limit_pct = risk.daily_loss_limit_pct
        row.max_open_positions = risk.max_open_positions
        row.max_open_positions_per_symbol = risk.max_open_positions_per_symbol
        row.exposure_cap_usdt = risk.exposure_cap_usdt
        row.leverage_ceiling = risk.leverage_ceiling
        row.conflicting_signal_policy = risk.conflicting_signal_policy
        # W9 KPI-Guard auto-pause thresholds (full-replace, like the limits above).
        row.kpi_guard_enabled = bool(risk.kpi_guard_enabled)
        row.kpi_guard_max_dd_pct = risk.kpi_guard_max_dd_pct
        row.kpi_guard_max_daily_loss_usdt = risk.kpi_guard_max_daily_loss_usdt
        row.kpi_guard_max_daily_loss_pct = risk.kpi_guard_max_daily_loss_pct
        row.kpi_guard_min_win_rate_pct = risk.kpi_guard_min_win_rate_pct
        row.kpi_guard_min_trades = risk.kpi_guard_min_trades
        # W9 Volatility Kill-Switch thresholds (full-replace, like the limits above).
        row.kill_switch_enabled = bool(risk.kill_switch_enabled)
        row.kill_switch_atr_spike_mult = risk.kill_switch_atr_spike_mult
        row.kill_switch_atr_period = risk.kill_switch_atr_period
        row.kill_switch_price_move_pct = risk.kill_switch_price_move_pct
        row.kill_switch_cooldown_seconds = risk.kill_switch_cooldown_seconds
        # B6 (W12) Strategy Anomaly Detection thresholds (full-replace).
        row.anomaly_detection_enabled = bool(risk.anomaly_detection_enabled)
        row.anomaly_z_threshold = risk.anomaly_z_threshold
        row.anomaly_window = risk.anomaly_window
        # B5 (W10) Promotion KPI-Gate thresholds (full-replace).
        row.promote_min_win_rate_pct = risk.promote_min_win_rate_pct
        row.promote_max_dd_pct = risk.promote_max_dd_pct
        row.promote_min_trades = risk.promote_min_trades
        row.promote_min_sandbox_days = risk.promote_min_sandbox_days
        if commit:
            await session.commit()

    async def apply_risk_config_to_all_strategies(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        risk: AutoTradeRiskConfigSchema,
    ) -> int:
        """A2 (audit §2): write one risk config to every strategy the user owns.

        Reuses the per-config :meth:`_apply_risk_config` (so the same validation,
        full-replace semantics and leverage-ceiling check apply) but stages all
        rows in a single transaction: if any config's venue can't support the
        requested ``leverage_ceiling`` the whole batch is rejected and nothing is
        persisted. Returns the number of strategies updated.
        """
        configs = list(
            (
                await session.scalars(
                    select(AutoTradeConfig).where(AutoTradeConfig.user_id == user_id)
                )
            ).all()
        )
        for config in configs:
            await self._apply_risk_config(
                session=session, config_id=config.id, risk=risk, commit=False
            )
        await session.commit()
        return len(configs)

    async def serialize_config(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
    ) -> AutoTradeConfigRead:
        """Build the API read model for a config, attaching its risk limits.

        Done explicitly (one extra ``session.get``) rather than via an ORM
        relationship: the codebase avoids lazy relationships, which are an
        async foot-gun (``MissingGreenlet`` on bare attribute access).
        """
        read = AutoTradeConfigRead.model_validate(config)
        risk = await self.get_risk_config(session=session, config_id=config.id)
        read.risk = AutoTradeRiskConfigSchema.model_validate(risk) if risk is not None else None
        return read

    # --- B5 (W10) Strategy Promotion Pipeline -------------------------------

    async def _account_is_demo(self, *, session: AsyncSession, account_id: int) -> bool:
        """True iff the strategy's exchange account is a *demo* account.

        The P4-4 safety invariant: a non-live (sandbox/validation) strategy may
        only run on a demo account — it accumulates real demo trades that feed
        the KPI gate via ``compute_strategy_health``, never touching real money.
        """
        mode = await session.scalar(
            select(ExchangeCredential.mode).where(ExchangeCredential.id == account_id)
        )
        return mode == EXCHANGE_MODE_DEMO

    def _sandbox_days(self, config: AutoTradeConfig) -> float:
        """Days the strategy has spent in sandbox (tenure for the KPI Gate).

        From ``sandbox_entered_at`` when set; otherwise falls back to the
        config's ``created_at`` (a config never explicitly sandboxed). Clamped
        to ``>= 0`` so clock skew can never *help* a strategy clear the gate.
        """
        anchor = config.sandbox_entered_at or config.created_at
        if anchor is None:
            return 0.0
        if anchor.tzinfo is None:
            # SQLite (and naive columns) return tz-naive datetimes; treat as UTC.
            anchor = anchor.replace(tzinfo=_utc_now().tzinfo)
        return max(0.0, (_utc_now() - anchor).total_seconds() / 86400.0)

    async def evaluate_promotion(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
    ) -> tuple[PromotionDecision, float]:
        """Compute the gate decision for a config (no side effects)."""
        risk = await self.get_risk_config(session=session, config_id=config.id)
        health = await compute_strategy_health(session=session, config_id=config.id)
        sandbox_days = self._sandbox_days(config)
        decision = evaluate_promotion_gate(
            health=health, risk_cfg=risk, sandbox_days=sandbox_days
        )
        return decision, sandbox_days

    async def get_promotion_status(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        config_id: int,
    ) -> PromotionStatusRead:
        """Current lifecycle stage + KPI-Gate readiness for one strategy."""
        config = await session.get(AutoTradeConfig, config_id)
        if config is None or config.user_id != user_id:
            raise LookupError("Auto-trade config not found.")
        decision, sandbox_days = await self.evaluate_promotion(session=session, config=config)
        return PromotionStatusRead(
            config_id=config.id,
            lifecycle_stage=config.lifecycle_stage,
            sandbox_days=sandbox_days,
            can_promote=decision.can_promote,
            criteria=[
                PromotionGateCriterionRead.model_validate(c) for c in decision.criteria
            ],
        )

    def _record_promotion_event(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        from_stage: str,
        to_stage: str,
        decision: str,
        kpi_snapshot: dict[str, object] | None,
        actor: str | None,
    ) -> None:
        """T19 (W10f): append a durable lifecycle-audit row. Added to the session
        in the same transaction as the stage change (the caller commits)."""
        session.add(
            StrategyPromotionEvent(
                user_id=config.user_id,
                config_id=config.id,
                from_stage=from_stage,
                to_stage=to_stage,
                decision=decision,
                kpi_snapshot_json=kpi_snapshot,
                actor=actor,
            )
        )

    async def list_promotion_events(
        self, *, session: AsyncSession, config_id: int
    ) -> list[StrategyPromotionEvent]:
        """Lifecycle-transition history for a config, newest first (T19)."""
        rows = await session.scalars(
            select(StrategyPromotionEvent)
            .where(StrategyPromotionEvent.config_id == config_id)
            .order_by(StrategyPromotionEvent.created_at.desc(), StrategyPromotionEvent.id.desc())
        )
        return list(rows)

    async def promote_strategy(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        config_id: int,
    ) -> AutoTradeConfig:
        """Promote a sandbox strategy to live — only when the KPI Gate passes.

        Honours the lifecycle FSM (sandbox → validation → live) and is gated by
        step-up at the API edge. On a failed gate it emits ``promotion_gate_failed``
        and raises :class:`PromotionGateError` (the strategy stays in sandbox).
        """
        stmt = self._with_for_update(
            session=session, stmt=select(AutoTradeConfig).where(AutoTradeConfig.id == config_id)
        )
        config = (await session.scalars(stmt)).first()
        if config is None or config.user_id != user_id:
            raise LookupError("Auto-trade config not found.")
        stage = LifecycleStage(config.lifecycle_stage)
        if stage is not LifecycleStage.SANDBOX:
            raise InvalidPromotionError(
                f"Only sandbox strategies can be promoted (stage={stage.value})."
            )

        decision, sandbox_days = await self.evaluate_promotion(session=session, config=config)
        if not decision.can_promote:
            self._record_promotion_event(
                session=session,
                config=config,
                from_stage=stage.value,
                to_stage=stage.value,
                decision="gate_failed",
                kpi_snapshot={
                    "sandbox_days": sandbox_days,
                    "criteria": _gate_criteria_payload(decision),
                },
                actor="user",
            )
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=None,
                position_id=None,
                event_type="promotion_gate_failed",
                level=EVENT_LEVEL_WARNING,
                message="Promotion gate not satisfied.",
                payload={
                    "sandbox_days": sandbox_days,
                    "failed": [c.name for c in decision.failed],
                },
                commit=True,
            )
            raise PromotionGateError(decision)

        # FSM: sandbox → validation → live (each transition validated).
        stage = apply_transition(stage, PromotionTrigger.REQUEST_PROMOTION)
        stage = apply_transition(stage, PromotionTrigger.GATE_PASSED)
        config.lifecycle_stage = stage.value
        config.sandbox_entered_at = None
        self._record_promotion_event(
            session=session,
            config=config,
            from_stage=LifecycleStage.SANDBOX.value,
            to_stage=stage.value,
            decision="promoted",
            kpi_snapshot={
                "sandbox_days": sandbox_days,
                "criteria": _gate_criteria_payload(decision),
            },
            actor="user",
        )
        await self._emit_event(
            session=session,
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            history_id=None,
            position_id=None,
            event_type="strategy_promoted",
            level=EVENT_LEVEL_INFO,
            message="Strategy promoted to live.",
            payload={
                "sandbox_days": sandbox_days,
                "criteria": _gate_criteria_payload(decision),
            },
            commit=True,
        )
        await session.refresh(config)
        return config

    async def demote_strategy(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        config_id: int,
    ) -> AutoTradeConfig:
        """Demote a live strategy back to sandbox (and stop it).

        Used on manual rollback or an anomaly auto-reaction (P4-8). Flips the
        stage via the FSM, re-stamps ``sandbox_entered_at`` (the tenure clock
        restarts), and halts live execution (``is_running=False``).
        """
        stmt = self._with_for_update(
            session=session, stmt=select(AutoTradeConfig).where(AutoTradeConfig.id == config_id)
        )
        config = (await session.scalars(stmt)).first()
        if config is None or config.user_id != user_id:
            raise LookupError("Auto-trade config not found.")
        from_stage = config.lifecycle_stage
        stage = apply_transition(LifecycleStage(config.lifecycle_stage), PromotionTrigger.DEMOTE)
        now = _utc_now()
        config.lifecycle_stage = stage.value
        config.sandbox_entered_at = now
        if config.is_running:
            config.is_running = False
            config.last_stopped_at = now
        self._record_promotion_event(
            session=session,
            config=config,
            from_stage=from_stage,
            to_stage=stage.value,
            decision="demoted",
            kpi_snapshot=None,
            actor="user",
        )
        await self._emit_event(
            session=session,
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            history_id=None,
            position_id=None,
            event_type="strategy_demoted",
            level=EVENT_LEVEL_WARNING,
            message="Strategy demoted to sandbox.",
            payload={},
            commit=True,
        )
        await session.refresh(config)
        return config

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
        # P4-4 safety gate: a non-live (sandbox/validation) strategy may run, but
        # ONLY on a demo account — it builds its KPI track record on demo trades
        # (read by the gate via compute_strategy_health) and must be promoted
        # through the KPI gate (step-up) before it can run on a real account.
        if (
            is_running
            and row.lifecycle_stage != LifecycleStage.LIVE.value
            and not await self._account_is_demo(session=session, account_id=row.account_id)
        ):
            raise ValueError(
                f"A non-live strategy (stage={row.lifecycle_stage}) may only run on a "
                "demo account; promote it through the KPI gate to run on a real account."
            )

        row.is_running = is_running
        if is_running:
            row.last_started_at = now
            # A3: a manual resume clears the kill-switch risk-off latch — the
            # operator has acknowledged the volatility trip and chosen to re-arm.
            row.risk_off_latched = False
            row.risk_off_reason = None
            row.risk_off_at = None
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

    async def set_running_bulk(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        is_running: bool,
    ) -> dict[str, Any]:
        """Flip ``is_running`` on every strategy the user owns (best-effort).

        W7 bulk-lifecycle helper. Disabled configs are skipped silently when
        starting (matches the per-config ``set_running`` rule: starting a
        disabled config raises). Stop-all does not skip — disabling already-
        stopped configs is a no-op so it is safe to flip.

        The per-config logic runs in its own try/except so one failure does
        not abort the others; the response contains a per-config outcome
        list. An aggregated audit event is emitted at the end.
        """

        configs = list(
            (
                await session.scalars(
                    select(AutoTradeConfig)
                    .where(AutoTradeConfig.user_id == user_id)
                    .order_by(AutoTradeConfig.id.asc())
                )
            ).all()
        )
        results: list[dict[str, Any]] = []
        succeeded = 0
        skipped = 0
        failed = 0
        for config in configs:
            outcome: dict[str, Any] = {
                "config_id": config.id,
                "account_id": config.account_id,
                "strategy_name": config.strategy_name,
            }
            if is_running and not config.enabled:
                outcome["status"] = "skipped"
                outcome["reason"] = "config_disabled"
                skipped += 1
                results.append(outcome)
                continue
            if config.is_running == is_running:
                outcome["status"] = "skipped"
                outcome["reason"] = "already_in_state"
                skipped += 1
                results.append(outcome)
                continue
            try:
                await self.set_running(
                    session=session,
                    user_id=user_id,
                    is_running=is_running,
                    account_id=config.account_id,
                )
                outcome["status"] = "ok"
                succeeded += 1
            except (ValueError, LookupError) as exc:
                outcome["status"] = "failed"
                outcome["error"] = str(exc)
                failed += 1
            results.append(outcome)

        await self._emit_event(
            session=session,
            user_id=user_id,
            config_id=None,
            profile_id=None,
            history_id=None,
            position_id=None,
            event_type="bulk_play_all_invoked" if is_running else "bulk_stop_all_invoked",
            level=EVENT_LEVEL_INFO,
            message=(
                "Bulk play-all invoked." if is_running else "Bulk stop-all invoked."
            ),
            payload={
                "requested": len(configs),
                "succeeded": succeeded,
                "skipped": skipped,
                "failed": failed,
            },
            commit=True,
        )
        return {
            "requested": len(configs),
            "succeeded": succeeded,
            "skipped": skipped,
            "failed": failed,
            "results": results,
        }

    async def _auto_pause_strategy(
        self,
        *,
        session: AsyncSession,
        config_id: int,
        trigger_event_type: str,
        message: str,
        payload: dict[str, Any],
        commit: bool = True,
    ) -> bool:
        """System-initiated strategy pause (KPI-Guard / kill-switch). Idempotent.

        Locks the config row and, only if it is currently running, flips
        ``is_running=False`` + stamps ``last_stopped_at``, then emits two events:
        the caller's trigger event (``trigger_event_type`` — e.g.
        ``kpi_guard_triggered``) carrying the breach ``payload``, and the generic
        ``strategy_auto_paused`` action. Returns ``True`` if it paused, ``False``
        if the strategy was already stopped (a clean no-op, no events) — so the
        guard cron, the on-close fast path and the kill-switch can all call it
        without double-pausing or double-logging. Deliberately distinct from the
        user-facing ``auto_trade_stop`` event so a system halt is auditable.

        ``commit=False`` lets the on-close fast path join the close's transaction
        (pause + close persist atomically); the cron leaves it ``True`` (terminal).
        """
        stmt = select(AutoTradeConfig).where(AutoTradeConfig.id == config_id)
        stmt = self._with_for_update(session=session, stmt=stmt)
        row = (await session.scalars(stmt)).first()
        if row is None or not row.is_running:
            return False
        row.is_running = False
        row.last_stopped_at = _utc_now()
        await self._emit_event(
            session=session,
            user_id=row.user_id,
            config_id=row.id,
            profile_id=row.profile_id,
            history_id=None,
            position_id=None,
            event_type=trigger_event_type,
            level=EVENT_LEVEL_WARNING,
            message=message,
            payload=payload,
            commit=False,
        )
        await self._emit_event(
            session=session,
            user_id=row.user_id,
            config_id=row.id,
            profile_id=row.profile_id,
            history_id=None,
            position_id=None,
            event_type="strategy_auto_paused",
            level=EVENT_LEVEL_WARNING,
            message="Strategy auto-paused by risk governance.",
            payload={"trigger": trigger_event_type, **payload},
            commit=commit,
        )
        return True

    async def _compute_guard_loss_inputs(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig | None,
        risk_cfg: AutoTradeRiskConfig | None,
    ) -> tuple[float, float | None]:
        """Inputs for the daily-loss KPI-Guard rule, computed *lazily*.

        Mirrors the pre-trade gate's optimization: today's realized PnL (pure DB,
        no exchange) is computed only when a daily-loss threshold is configured,
        and the sub-account balance (the only exchange call) is fetched only when
        the pct rule can actually fire — i.e. there is a realized loss today.
        Returns ``(today_realized_pnl_usdt, account_balance_usdt | None)``.
        """
        if config is None or risk_cfg is None or not risk_cfg.kpi_guard_enabled:
            return 0.0, None
        needs_usdt = risk_cfg.kpi_guard_max_daily_loss_usdt is not None
        needs_pct = risk_cfg.kpi_guard_max_daily_loss_pct is not None
        if not (needs_usdt or needs_pct):
            return 0.0, None
        today_pnl = await self._today_realized_pnl_usdt(session=session, config_id=config.id)
        balance: float | None = None
        if needs_pct and today_pnl < 0:
            balance = await self._safe_subaccount_usdt_balance(
                session=session, user_id=config.user_id, account_id=config.account_id
            )
        return today_pnl, balance

    async def apply_kpi_guard(
        self,
        *,
        session: AsyncSession,
        config_id: int,
        health: StrategyHealth,
        commit: bool = True,
    ) -> GuardDecision:
        """Evaluate the KPI-Guard for one strategy and auto-pause on breach.

        Takes a *precomputed* ``health`` because the caller (the W9 guard cron in
        T1.3, the on-close fast path in T1.4) already has it — avoiding a second
        ``compute_strategy_health``. The daily-loss inputs are computed here
        (lazily). The decision is pure (``evaluate_kpi_guard``); the pause side
        effect is idempotent (``_auto_pause_strategy``). ``commit=False`` joins the
        caller's transaction (the on-close path). A ``warning`` (the pct rule
        skipped on an unavailable balance) is surfaced as ``risk_check_degraded``
        and never causes a pause (fail-open, SPEC §6.3).
        """
        config = await session.get(AutoTradeConfig, config_id)
        risk_cfg = await self.get_risk_config(session=session, config_id=config_id)
        today_pnl, balance = await self._compute_guard_loss_inputs(
            session=session, config=config, risk_cfg=risk_cfg
        )
        decision = evaluate_kpi_guard(
            health=health,
            risk_cfg=risk_cfg,
            today_realized_pnl_usdt=today_pnl,
            account_balance_usdt=balance,
        )
        if decision.warning and config is not None:
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=None,
                position_id=None,
                event_type="risk_check_degraded",
                level=EVENT_LEVEL_WARNING,
                message=decision.warning,
                payload={"source": "kpi_guard", "rule": "daily_loss_pct"},
                commit=False,
            )
        if decision.should_pause:
            await self._auto_pause_strategy(
                session=session,
                config_id=config_id,
                trigger_event_type="kpi_guard_triggered",
                message=f"KPI-Guard breached: {', '.join(decision.rules)}.",
                payload={
                    "source": "kpi_guard",
                    "health_score": health.health_score,
                    "sample_size": health.sample_size,
                    "breaches": [
                        {"rule": b.rule, "actual": b.actual, "threshold": b.threshold}
                        for b in decision.breaches
                    ],
                },
                commit=False,
            )
        # Single atomic commit (review I1): self-contained for commit=True so a
        # degraded-only outcome (warning, no pause) is persisted too; commit=False
        # joins the caller's transaction (the on-close fast path).
        if commit:
            await session.commit()
        return decision

    async def sweep_kpi_guards(self, *, session: AsyncSession) -> dict[str, int]:
        """Evaluate the KPI-Guard for every RUNNING strategy; auto-pause on breach.

        W9 guard cron entry (every 5 min). **Best-effort**: each config runs in its
        own try/except + rollback so one failure cannot abort the sweep (like
        ``set_running_bulk``). Health is computed in-process from stored prices
        (no exchange call — review C1); one snapshot is recorded per evaluated
        config — the history the KPI-Guard and the AC#7 dashboard read.
        """
        config_ids = list(
            (
                await session.scalars(
                    select(AutoTradeConfig.id)
                    .where(
                        AutoTradeConfig.enabled.is_(True),
                        AutoTradeConfig.is_running.is_(True),
                    )
                    .order_by(AutoTradeConfig.id.asc())
                )
            ).all()
        )
        retention_cutoff = _utc_now() - timedelta(
            days=get_settings().strategy_health_snapshot_retention_days
        )
        evaluated = 0
        paused = 0
        errors = 0
        for config_id in config_ids:
            try:
                config = await session.get(AutoTradeConfig, config_id)
                # Re-check under the fresh read: a prior iteration / concurrent
                # action may have stopped it since the id list was taken.
                if config is None or not config.is_running:
                    continue
                health = await compute_strategy_health(session=session, config_id=config_id)
                await record_health_snapshot(
                    session=session, health=health, user_id=config.user_id
                )
                # Retention: drop this config's snapshots beyond the window (review S1).
                await prune_strategy_health_snapshots(
                    session=session, config_id=config_id, cutoff=retention_cutoff
                )
                decision = await self.apply_kpi_guard(
                    session=session, config_id=config_id, health=health
                )
                await session.commit()
                evaluated += 1
                if decision.should_pause:
                    paused += 1
            except Exception:
                errors += 1
                await session.rollback()
                logger.exception("kpi_guard sweep failed for config_id=%s", config_id)
        return {"evaluated": evaluated, "paused": paused, "errors": errors}

    async def sweep_strategy_anomalies(
        self, *, session: AsyncSession, series_limit: int = 200
    ) -> dict[str, int]:
        """Detect anomalies for every RUNNING strategy with detection enabled (B6).

        Sweep cron entry (every 15 min). **Best-effort**: each config runs in its
        own try/except + rollback so one failure cannot abort the sweep. The
        series is bounded to the last ``series_limit`` closed trades (never the
        whole ledger). High/critical findings emit one
        ``strategy_anomaly_detected`` event, deduped by a per-config cooldown so
        the same standing anomaly does not re-alert every tick.
        """
        cooldown = timedelta(minutes=60)
        config_ids = list(
            (
                await session.scalars(
                    select(AutoTradeConfig.id)
                    .join(
                        AutoTradeRiskConfig,
                        AutoTradeRiskConfig.config_id == AutoTradeConfig.id,
                    )
                    .where(
                        AutoTradeConfig.enabled.is_(True),
                        AutoTradeConfig.is_running.is_(True),
                        AutoTradeRiskConfig.anomaly_detection_enabled.is_(True),
                    )
                    .order_by(AutoTradeConfig.id.asc())
                )
            ).all()
        )
        evaluated = 0
        alerted = 0
        errors = 0
        for config_id in config_ids:
            try:
                config = await session.get(AutoTradeConfig, config_id)
                if config is None or not config.is_running:
                    continue
                risk_cfg = await session.get(AutoTradeRiskConfig, config_id)
                if risk_cfg is None or not risk_cfg.anomaly_detection_enabled:
                    continue
                realized_pnls, bucket_counts = await self._load_anomaly_series(
                    session=session, config_id=config_id, limit=series_limit
                )
                findings = detect_anomalies(
                    trade_pnls=realized_pnls,
                    bucket_counts=bucket_counts or None,
                    cfg=_anomaly_config_from_risk(risk_cfg),
                )
                evaluated += 1
                if not findings:
                    continue
                # Dedup: skip if this config already alerted within the cooldown.
                recent = await session.scalar(
                    select(func.count())
                    .select_from(AutoTradeEvent)
                    .where(
                        AutoTradeEvent.config_id == config_id,
                        AutoTradeEvent.event_type == "strategy_anomaly_detected",
                        AutoTradeEvent.created_at >= _utc_now() - cooldown,
                    )
                )
                if recent:
                    continue
                severity = (
                    "critical"
                    if any(f.severity == "critical" for f in findings)
                    else "warning"
                )
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=None,
                    position_id=None,
                    event_type="strategy_anomaly_detected",
                    level=EVENT_LEVEL_WARNING,
                    message=f"Strategy anomaly detected ({severity}).",
                    payload={
                        "severity": severity,
                        "findings": [
                            {
                                "metric": f.metric,
                                "value": f.value,
                                "baseline": f.baseline,
                                "z_score": f.z_score,
                                "severity": f.severity,
                            }
                            for f in findings
                        ],
                    },
                    commit=True,
                )
                alerted += 1
            except Exception:
                errors += 1
                await session.rollback()
                logger.exception("anomaly sweep failed for config_id=%s", config_id)
        return {"evaluated": evaluated, "alerted": alerted, "errors": errors}

    async def _load_anomaly_series(
        self, *, session: AsyncSession, config_id: int, limit: int
    ) -> tuple[list[float], list[float]]:
        """Bounded per-trade realized-PnL series + per-day trade counts (oldest first).

        Uses **gross** realized PnL (``gross_realized_pnl`` — the same basis the
        Health Score uses) as the per-trade series. True net (minus commissions,
        plus funding) would need the W9 ledger join; the detector measures only
        *relative* deviation, so gross is an adequate proxy here.
        """
        rows = (
            await session.scalars(
                select(AutoTradePosition)
                .where(
                    AutoTradePosition.config_id == config_id,
                    AutoTradePosition.status == "closed",
                )
                .order_by(AutoTradePosition.closed_at.desc())
                .limit(limit)
            )
        ).all()
        realized_pnls: list[float] = []
        day_counts: dict[Any, int] = {}
        for position in reversed(list(rows)):
            realized = gross_realized_pnl(position)
            if realized is None or not math.isfinite(realized):
                continue
            realized_pnls.append(realized)
            if position.closed_at is not None:
                day = position.closed_at.date()
                day_counts[day] = day_counts.get(day, 0) + 1
        bucket_counts = [float(day_counts[d]) for d in sorted(day_counts)]
        return realized_pnls, bucket_counts

    async def sweep_promotion_gates(self, *, session: AsyncSession) -> dict[str, int]:
        """Auto-evaluate the KPI Gate for every SANDBOX strategy (B5 — P4-3).

        Sweep cron entry (every 30 min). For each enabled, **running** sandbox
        strategy whose gate now passes, emit one ``promotion_ready`` event (SSE +
        Telegram) so a human can promote it (the actual sandbox→live move stays
        step-up gated — the cron never auto-promotes). Gated on ``is_running``
        like the other sweeps: an idle sandbox strategy is not actively proving
        itself, so re-running ``compute_strategy_health`` for it is wasted work.
        Best-effort per config; deduped by a per-config cooldown so a
        standing-ready strategy does not re-notify every tick.

        Audit note: lifecycle transitions are recorded via the
        ``promotion_ready`` / ``strategy_promoted`` / ``strategy_demoted`` /
        ``promotion_gate_failed`` events (with a gate-criteria snapshot), not a
        dedicated ``strategy_promotion_events`` table — sufficient for M4; a
        first-class lifecycle-history table is deferred.
        """
        cooldown = timedelta(hours=6)
        config_ids = list(
            (
                await session.scalars(
                    select(AutoTradeConfig.id)
                    .where(
                        AutoTradeConfig.enabled.is_(True),
                        AutoTradeConfig.is_running.is_(True),
                        AutoTradeConfig.lifecycle_stage == LifecycleStage.SANDBOX.value,
                    )
                    .order_by(AutoTradeConfig.id.asc())
                )
            ).all()
        )
        evaluated = 0
        ready = 0
        errors = 0
        for config_id in config_ids:
            try:
                config = await session.get(AutoTradeConfig, config_id)
                if config is None or config.lifecycle_stage != LifecycleStage.SANDBOX.value:
                    continue
                decision, sandbox_days = await self.evaluate_promotion(
                    session=session, config=config
                )
                evaluated += 1
                if not decision.can_promote:
                    continue
                recent = await session.scalar(
                    select(func.count())
                    .select_from(AutoTradeEvent)
                    .where(
                        AutoTradeEvent.config_id == config_id,
                        AutoTradeEvent.event_type == "promotion_ready",
                        AutoTradeEvent.created_at >= _utc_now() - cooldown,
                    )
                )
                if recent:
                    continue
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=None,
                    position_id=None,
                    event_type="promotion_ready",
                    level=EVENT_LEVEL_INFO,
                    message="Strategy is ready for promotion to live.",
                    payload={"sandbox_days": sandbox_days},
                    commit=True,
                )
                ready += 1
            except Exception:
                errors += 1
                await session.rollback()
                logger.exception("promotion gate sweep failed for config_id=%s", config_id)
        return {"evaluated": evaluated, "ready": ready, "errors": errors}

    async def sweep_portfolio_dd_guards(self, *, session: AsyncSession) -> dict[str, int]:
        """Portfolio-DD watcher: pause ALL of a user's strategies on portfolio drawdown.

        Phase-1 B2. Ships behind ``portfolio_dd_halt_enabled`` (off by default — it
        acts on real money, so the threshold must be calibrated with traders before
        a human flips it on). For every user with running strategies it computes the
        true **merged-equity** portfolio drawdown (T12/W11a) — one equity curve over
        every strategy's closed trades, not the worst single strategy's proxy. On breach it
        flips every strategy off via ``set_running_bulk`` and emits one user-level
        ``portfolio_dd_halt`` risk event (config_id=None), which the Telegram outbox
        delivers. **Best-effort**: each user runs in its own try/except + rollback so
        one failure cannot abort the sweep (like ``sweep_kpi_guards``). Naturally
        idempotent: once halted there are no running strategies left to re-trigger.
        """
        settings = get_settings()
        if not settings.portfolio_dd_halt_enabled:
            return {"users": 0, "halted": 0, "errors": 0}
        threshold = settings.portfolio_dd_halt_threshold_pct
        # Defense-in-depth: Settings validates threshold in (0, 100], but never let a
        # non-positive value reach the breach gate — worst_dd seeds at 0.0, so
        # ``0.0 < 0.0`` is False and the watcher would mass-halt every running strategy.
        if threshold <= 0:
            logger.error(
                "portfolio_dd watcher: non-positive threshold %s — skipping sweep", threshold
            )
            return {"users": 0, "halted": 0, "errors": 0}

        user_ids = list(
            (
                await session.scalars(
                    select(AutoTradeConfig.user_id)
                    .where(
                        AutoTradeConfig.enabled.is_(True),
                        AutoTradeConfig.is_running.is_(True),
                    )
                    .distinct()
                    .order_by(AutoTradeConfig.user_id.asc())
                )
            ).all()
        )
        halted = 0
        errors = 0
        for user_id in user_ids:
            try:
                from app.services.auto_trade.portfolio import (
                    compute_merged_portfolio_dd_pct,
                )

                configs = list(
                    (
                        await session.scalars(
                            select(AutoTradeConfig)
                            .where(
                                AutoTradeConfig.user_id == user_id,
                                AutoTradeConfig.enabled.is_(True),
                                AutoTradeConfig.is_running.is_(True),
                            )
                            .order_by(AutoTradeConfig.id.asc())
                        )
                    ).all()
                )
                # T12 (W11a): true merged-equity portfolio drawdown across ALL of the
                # user's running strategies — catches portfolio-wide bleed that no
                # single strategy breaches (the old worst-strategy proxy missed it).
                portfolio_dd = await compute_merged_portfolio_dd_pct(
                    session=session,
                    configs=configs,
                    cutoff=datetime.now(UTC) - timedelta(days=30),
                )
                if portfolio_dd < threshold:
                    continue
                result = await self.set_running_bulk(
                    session=session, user_id=user_id, is_running=False
                )
                # Emit only when an actual halt happened (avoids spamming when the
                # strategies were already stopped between the id read and the flip).
                if int(result["succeeded"]) > 0:
                    # The halt is already committed by set_running_bulk, so count it
                    # now. The alert event is a *separate* transaction: if its emit
                    # fails the strategies must stay paused (the safety action won)
                    # and the lost notification must be LOUD, not silent — this is a
                    # real-money control. (True single-transaction atomicity would
                    # require threading commit through set_running_bulk; deferred.)
                    halted += 1
                    try:
                        await self._emit_event(
                            session=session,
                            user_id=user_id,
                            config_id=None,
                            profile_id=None,
                            history_id=None,
                            position_id=None,
                            event_type="portfolio_dd_halt",
                            level=EVENT_LEVEL_WARNING,
                            message=(
                                f"Portfolio drawdown {portfolio_dd:.2f}% breached the "
                                f"{threshold:.2f}% halt threshold — all strategies paused."
                            ),
                            payload={
                                "source": "portfolio_dd",
                                "portfolio_dd_pct": portfolio_dd,
                                "threshold_pct": threshold,
                                "paused_count": int(result["succeeded"]),
                                "basis": "merged_equity",
                            },
                            commit=True,
                        )
                    except Exception:
                        await session.rollback()
                        logger.critical(
                            "portfolio_dd_halt committed for user_id=%s but alert emit "
                            "failed — strategies are paused with NO notification",
                            user_id,
                            exc_info=True,
                        )
            except Exception:
                errors += 1
                await session.rollback()
                logger.exception("portfolio_dd sweep failed for user_id=%s", user_id)
        return {"users": len(user_ids), "halted": halted, "errors": errors}

    async def push_portfolio_kpis(self, *, session: AsyncSession) -> dict[str, int]:
        """Push a portfolio KPI snapshot over SSE to every user with running strategies.

        T15 (W12g): so the Live Monitor reads KPI numbers from the stream rather than
        polling. The payload is the same shape as ``GET /auto-trade/portfolio``
        (``PortfolioSummaryResponse``) so the frontend reuses its parser. Best-effort:
        ``fetch_balances=False`` (no exchange round-trips on the cron path) and per-user
        try/except so one user's failure can't abort the sweep. ``publish_user_event``
        never raises. Read-only / user-scoped — no safety flag needed.
        """
        from app.schemas.auto_trade import PortfolioSummaryResponse
        from app.services.auto_trade.portfolio import compute_portfolio

        user_ids = list(
            (
                await session.scalars(
                    select(AutoTradeConfig.user_id)
                    .where(
                        AutoTradeConfig.enabled.is_(True),
                        AutoTradeConfig.is_running.is_(True),
                    )
                    .distinct()
                    .order_by(AutoTradeConfig.user_id.asc())
                )
            ).all()
        )
        pushed = 0
        for user_id in user_ids:
            try:
                summary = await compute_portfolio(
                    session=session,
                    auto_trade=self,
                    trading=self._trading,
                    user_id=user_id,
                    fetch_balances=False,
                    include_merged_dd=False,  # I5: DD comes from the slower poll
                )
                payload = PortfolioSummaryResponse.model_validate(summary).model_dump(mode="json")
                await publish_user_event(
                    user_id=user_id, event_type="portfolio_kpi", payload=payload
                )
                pushed += 1
            except Exception:
                logger.exception("portfolio_kpi push failed for user_id=%s", user_id)
        return {"users": len(user_ids), "pushed": pushed}

    async def _maybe_auto_pause_after_close(self, *, session: AsyncSession, config_id: int) -> None:
        """Fast-path KPI-Guard check right after a position closes.

        So a catastrophic loss/drawdown pauses the strategy **within the same
        transaction as the close** instead of waiting up to 5 min for the guard
        cron (``commit=False`` — the close flow commits). Idempotent with the cron
        (``_auto_pause_strategy`` no-ops an already-stopped strategy). The guard
        is opt-in, so the (cheap, PK) risk-config lookup short-circuits before any
        health compute when it is off — no cost on the common path. **Best-effort**:
        a guard error is logged and swallowed so it can never abort the close it
        follows.
        """
        try:
            config = await session.get(AutoTradeConfig, config_id)
            if config is None or not config.is_running:
                return
            risk_cfg = await self.get_risk_config(session=session, config_id=config_id)
            if risk_cfg is None or not risk_cfg.kpi_guard_enabled:
                return
            health = await compute_strategy_health(session=session, config_id=config_id)
            await self.apply_kpi_guard(
                session=session, config_id=config_id, health=health, commit=False
            )
        except Exception:
            logger.exception("post-close KPI-Guard check failed for config_id=%s", config_id)

    async def kill_switch_close_position(
        self,
        *,
        session: AsyncSession,
        position_id: int,
        signal: KillSwitchSignal,
        commit: bool = False,
    ) -> bool:
        """Hard market reduce-only close of an open position tripped by the
        Volatility Kill-Switch (AC#4, in-trade).

        Reuses ``_flatten_single_position`` (no parallel close path) so the DB
        ``state``→CLOSED, the exchange reduce-only close and the ledger are all
        handled there; stamps ``close_reason="volatility_kill_switch"``.
        **Idempotent**: a non-open position is a clean no-op (so the realtime hook
        and any reconciliation can't double-close). **Best-effort**: a close
        failure is recorded (``kill_switch_triggered``, error level) and returned
        as ``False`` — never retried into a loop here (the realtime per-position
        cooldown guards re-entry). ``commit=False`` joins the caller's transaction.
        """
        # Row-lock the position (review S4) so concurrent trips can't both pass the
        # open-check and double-close (no-op on SQLite; SELECT FOR UPDATE on Postgres).
        stmt = select(AutoTradePosition).where(AutoTradePosition.id == position_id)
        stmt = self._with_for_update(session=session, stmt=stmt)
        position = (await session.scalars(stmt)).first()
        if position is None or position.status != POSITION_OPEN:
            return False
        config = await session.get(AutoTradeConfig, position.config_id)
        if config is None:
            return False
        await self._flatten_single_position(
            session=session,
            config=config,
            position_row=position,
            reason="volatility_kill_switch",
        )
        closed_ok = position.status == POSITION_CLOSED
        await self._emit_event(
            session=session,
            user_id=config.user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            history_id=None,
            position_id=position.id,
            event_type="kill_switch_triggered",
            level=EVENT_LEVEL_WARNING if closed_ok else EVENT_LEVEL_ERROR,
            message=(
                f"Volatility kill-switch ({signal.reason}): "
                + ("position closed." if closed_ok else "CLOSE FAILED.")
            ),
            payload={
                "source": "kill_switch",
                "reason": signal.reason,
                "actual": signal.actual,
                "threshold": signal.threshold,
                "closed": closed_ok,
            },
            commit=False,
        )
        # Latch the strategy risk-off: a volatility trip halts NEW entries until a
        # human re-enables (set_running(True)) — regardless of whether the close
        # itself succeeded (a failed close leaves an open position, so we must
        # still stop opening more). Reuses the idempotent pause, so a config the
        # KPI-Guard already paused is a clean no-op (no duplicate events).
        await self._auto_pause_strategy(
            session=session,
            config_id=config.id,
            trigger_event_type="risk_off_entered",
            message=f"Risk-off latched by volatility kill-switch ({signal.reason}).",
            payload={"source": "kill_switch", "reason": signal.reason, "closed": closed_ok},
            commit=False,
        )
        # A3: persist the risk-off latch on the config so it survives a restart and
        # is visible to operators/UI. Set unconditionally (even on a failed close or
        # an already-paused strategy) — a confirmed spike means "no new entries".
        config.risk_off_latched = True
        config.risk_off_reason = signal.reason
        config.risk_off_at = _utc_now()
        # Single atomic commit for the close + all events (the realtime caller
        # passes commit=False to join its own transaction).
        if commit:
            await session.commit()
        return closed_ok

    async def _apply_kill_switch_config(
        self, *, session: AsyncSession | None, position: PositionContext
    ) -> None:
        """Copy the strategy's Volatility Kill-Switch config onto the runtime
        ``PositionContext`` (W9 T2.3b) so ``RealtimeSLAdjuster.on_tick`` can evaluate
        the spike detector. Fail-safe: no session, an unresolvable position id, a
        missing position row, or no/disabled risk row leaves the kill-switch off.
        """
        if session is None:
            return
        try:
            position_id = int(position.position_id)
        except (TypeError, ValueError):
            return
        row = await session.get(AutoTradePosition, position_id)
        if row is None:
            return
        risk_cfg = await self.get_risk_config(session=session, config_id=row.config_id)
        if risk_cfg is None or not risk_cfg.kill_switch_enabled:
            return
        position.kill_switch_enabled = True
        position.kill_switch_atr_spike_mult = risk_cfg.kill_switch_atr_spike_mult
        position.kill_switch_atr_period = risk_cfg.kill_switch_atr_period
        position.kill_switch_price_move_pct = risk_cfg.kill_switch_price_move_pct
        position.kill_switch_cooldown_seconds = risk_cfg.kill_switch_cooldown_seconds

    async def _runtime_kill_switch_close(
        self, position: PositionContext, signal: KillSwitchSignal
    ) -> None:
        """Production kill-switch handler wired into the realtime tracker (W9 T2.3b).

        The realtime tick has no DB session; this opens one and hard-closes the
        position via the tested ``kill_switch_close_position`` (which closes + emits
        + latches risk-off, idempotently).
        """
        try:
            position_id = int(position.position_id)
        except (TypeError, ValueError):
            logger.warning(
                "kill-switch: non-numeric position_id %r; skipping close.",
                position.position_id,
            )
            return
        async with AsyncSessionFactory() as session:
            await self.kill_switch_close_position(
                session=session, position_id=position_id, signal=signal, commit=True
            )

    async def close_open_positions(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int | None = None,
        confirm: bool,
        reason: str | None = None,
    ) -> AutoTradeCloseOpenPositionsResponse:
        """Manually flatten every open ``AutoTradePosition`` for the user/account.

        Two-step flow:
          * ``confirm=False`` → raises :class:`ConfirmationRequiredError` with
            an :class:`AutoTradeClosePreview` payload. Nothing changes.
          * ``confirm=True``  → cancels all known TP/SL conditional orders
            then issues a market reduce-only close per position. Failures on
            individual symbols do not abort the loop — partial outcomes are
            returned in the response.

        Decoupled from ``set_running`` (auto-trade keeps its enabled state).
        Pending signal-queue rows are also untouched: the user explicitly
        chose this separation in the design discussion.
        """
        config = await self._get_config_for_scope(
            session=session,
            user_id=user_id,
            account_id=account_id,
            fail_on_ambiguous=True,
            lock_for_update=False,
        )
        if config is None:
            raise LookupError("Auto-trade config not found.")

        scope_account_id = config.account_id

        position_rows = await self._fetch_open_positions_for_close(
            session=session,
            user_id=user_id,
            account_id=scope_account_id,
        )

        if not confirm:
            preview_items = []
            for row in position_rows:
                live_count, live_sl = await self._resolve_conditional_summary(
                    session=session, position_row=row
                )
                db_sl = float(row.sl_price) if row.sl_price is not None else None
                preview_items.append(
                    AutoTradeClosePreviewItem(
                        position_id=row.id,
                        symbol=row.symbol,
                        side=cast(Any, row.side),
                        current_quantity=float(row.current_quantity or row.quantity or 0.0),
                        entry_price=float(row.entry_price),
                        # Prefer the live SL trigger from the exchange; fall back
                        # to the DB value (which can be a stale entry-price
                        # default for positions opened/reconciled out of band).
                        current_sl_price=live_sl if live_sl is not None else db_sl,
                        open_conditional_orders_count=live_count,
                    )
                )
            preview = AutoTradeClosePreview(
                positions=preview_items,
                total_count=len(preview_items),
            )
            raise ConfirmationRequiredError(preview)

        result = AutoTradeCloseOpenPositionsResponse()

        if not position_rows:
            await self._emit_event(
                session=session,
                user_id=user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=None,
                position_id=None,
                event_type="auto_trade_close_positions_completed",
                level=EVENT_LEVEL_INFO,
                message="No open auto-trade positions to close.",
                payload={"closed": 0, "failed": 0, "reason": reason},
                commit=True,
            )
            return result

        for row in position_rows:
            outcome = await self._flatten_single_position(
                session=session,
                config=config,
                position_row=row,
                reason=reason,
            )
            if isinstance(outcome, AutoTradeClosedPositionInfo):
                result.closed.append(outcome)
            elif isinstance(outcome, AutoTradeFailedClosePositionInfo):
                result.failed.append(outcome)
            else:
                # Already-closed sentinel.
                result.skipped_already_closed.append(int(row.id))

        # Fast-path KPI-Guard after the flatten (same transaction as the completion
        # event below, commit=False) — realized losses here may breach the guard.
        await self._maybe_auto_pause_after_close(session=session, config_id=config.id)

        await self._emit_event(
            session=session,
            user_id=user_id,
            config_id=config.id,
            profile_id=config.profile_id,
            history_id=None,
            position_id=None,
            event_type="auto_trade_close_positions_completed",
            level=(
                EVENT_LEVEL_ERROR
                if result.failed
                else EVENT_LEVEL_INFO
            ),
            message=(
                f"Manual close: {len(result.closed)} closed, "
                f"{len(result.failed)} failed, "
                f"{len(result.skipped_already_closed)} already closed."
            ),
            payload={
                "closed_count": len(result.closed),
                "failed_count": len(result.failed),
                "skipped_count": len(result.skipped_already_closed),
                "reason": reason,
                "closed_position_ids": [item.position_id for item in result.closed],
                "failed_position_ids": [item.position_id for item in result.failed],
            },
            commit=True,
        )
        return result

    async def _fetch_open_positions_for_close(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
    ) -> list[AutoTradePosition]:
        """Return live ``AutoTradePosition`` rows eligible for manual flatten.

        We treat any non-terminal state as eligible so the operator can
        recover stuck positions (PENDING, ENTERING, RECONNECTING, etc.) just
        as well as steady-state OPEN ones.
        """
        terminal_states = {
            PositionState.CLOSED.value,
            PositionState.CANCELLED.value,
            PositionState.FAILED.value,
        }
        stmt = (
            select(AutoTradePosition)
            .where(AutoTradePosition.user_id == user_id)
            .where(AutoTradePosition.account_id == account_id)
            .where(AutoTradePosition.status == POSITION_OPEN)
            .where(AutoTradePosition.state.notin_(terminal_states))
            .order_by(AutoTradePosition.id.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _count_known_conditional_orders(row: AutoTradePosition) -> int:
        count = 0
        if row.sl_exchange_order_id:
            count += 1
        for level in row.tp_levels_json or []:
            if level.get("exchange_order_id") and level.get("status") != "triggered":
                count += 1
        return count

    async def _resolve_conditional_summary(
        self,
        *,
        session: AsyncSession,
        position_row: AutoTradePosition,
    ) -> tuple[int, float | None]:
        """Best-effort live conditional-order summary from the exchange.

        Returns ``(open_conditional_count, stop_loss_trigger_price)`` read from
        the exchange Algo endpoint (``get_open_conditional_orders`` →
        ``/fapi/v1/openAlgoOrders`` on Binance). Binance migrated conditional
        orders to the Algo Service (2025-12-09), so the DB-recorded ids and the
        legacy open-orders endpoint both under-report what is actually live for
        positions opened or reconciled outside the runtime — producing the
        "0 TP/SL orders" / "SL = entry" symptom in the close preview.

        Falls back to the DB-known count (and ``None`` SL) on any failure so a
        flaky exchange never breaks the preview.
        """
        db_count = self._count_known_conditional_orders(position_row)
        try:
            adapter = await self._create_exchange_adapter(
                session=session, position=cast(Any, position_row)
            )
            get_orders = getattr(adapter, "get_open_conditional_orders", None)
            if get_orders is None:
                return db_count, None
            orders = await get_orders(str(position_row.symbol))
        except Exception:
            logger.warning(
                "close-preview: live conditional-order lookup failed for position "
                "%s; falling back to DB count.",
                position_row.id,
                exc_info=True,
            )
            return db_count, None

        if not isinstance(orders, list):
            return db_count, None

        sl_price: float | None = None
        for order in orders:
            if getattr(order, "order_type", "") == "stop_loss":
                try:
                    sl_price = float(order.trigger_price)
                except (TypeError, ValueError):
                    sl_price = None
                break
        return len(orders), sl_price

    async def _flatten_single_position(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
        position_row: AutoTradePosition,
        reason: str | None,
    ) -> AutoTradeClosedPositionInfo | AutoTradeFailedClosePositionInfo | None:
        """Cancel known conditionals, market-close the position, persist state.

        Returns:
          * :class:`AutoTradeClosedPositionInfo` on success.
          * :class:`AutoTradeFailedClosePositionInfo` when market close fails.
          * ``None`` if the position is already flat on the exchange (skipped).
        """
        position_id = int(position_row.id)
        symbol = str(position_row.symbol)
        live_quantity = float(position_row.current_quantity or position_row.quantity or 0.0)
        side_raw = str(position_row.side or "").upper()
        position_side = (
            RuntimePositionSide.SHORT
            if side_raw == "SHORT"
            else RuntimePositionSide.LONG
        )
        closing_side = self._closing_exchange_order_side(position_side)

        # Build a thin context for adapter creation only — full from_db_row
        # would also reconstruct state machine etc., which we do not need
        # here because we are about to mark the row CLOSED unconditionally.
        adapter_position = PositionContext(
            position_id=str(position_id),
            user_id=str(position_row.user_id),
            account_id=str(position_row.account_id),
            symbol=symbol,
            side=position_side,
        )

        try:
            adapter = await self._create_exchange_adapter(
                session=session,
                position=adapter_position,
            )
        except Exception as exc:
            logger.exception(
                "close_open_positions: failed to create adapter for position %s",
                position_id,
            )
            return AutoTradeFailedClosePositionInfo(
                position_id=position_id,
                symbol=symbol,
                error=f"adapter_init_failed: {exc}",
            )

        # 1. Cancel known TP/SL conditional orders (best-effort; failure to
        #    cancel one does not block the market close).
        cancelled_orders: list[str] = []
        for order_id in self._iter_known_conditional_order_ids(position_row):
            try:
                await adapter.cancel_conditional_order(symbol, order_id)
                cancelled_orders.append(order_id)
            except Exception:
                logger.exception(
                    "close_open_positions: failed to cancel conditional %s on %s",
                    order_id,
                    symbol,
                )

        # 2. Verify whether the position is already flat on the exchange.
        try:
            exchange_position = await adapter.get_position(symbol)
        except Exception:
            logger.exception(
                "close_open_positions: get_position failed for %s",
                symbol,
            )
            exchange_position = None

        live_size = (
            abs(float(getattr(exchange_position, "size", 0.0) or 0.0))
            if exchange_position is not None
            else live_quantity
        )

        if live_size <= _POSITION_EPSILON:
            self._mark_position_closed(
                position_row=position_row,
                close_reason=reason or "manual_close_already_flat",
            )
            await session.commit()
            self._untrack_position_in_ws_manager(position_row)
            await self._emit_event(
                session=session,
                user_id=int(position_row.user_id),
                config_id=int(position_row.config_id),
                profile_id=int(position_row.profile_id),
                history_id=position_row.close_history_id,
                position_id=position_id,
                event_type="position_manual_closed",
                level=EVENT_LEVEL_INFO,
                message="Position was already flat on exchange; marked closed.",
                payload={
                    "symbol": symbol,
                    "skipped_market_close": True,
                    "cancelled_conditional_orders": cancelled_orders,
                    "reason": reason,
                },
                commit=True,
            )
            return None

        # 3. Issue the market reduce-only close.
        client_order_id = self._build_position_runtime_order_id(
            position_id=str(position_id),
            kind="manual-close",
        )
        executed_qty = 0.0
        avg_price: float | None = None
        try:
            close_result = await adapter.partial_close(
                symbol=symbol,
                side=closing_side,
                quantity=live_size,
                client_order_id=client_order_id,
                order_type="market",
            )
            executed_qty = float(close_result.executed_qty)
            avg_price = (
                float(close_result.avg_price)
                if close_result.avg_price > 0
                else None
            )
        except Exception as exc:
            logger.exception(
                "close_open_positions: market close failed for %s",
                symbol,
            )
            await self._emit_event(
                session=session,
                user_id=int(position_row.user_id),
                config_id=int(position_row.config_id),
                profile_id=int(position_row.profile_id),
                history_id=position_row.close_history_id,
                position_id=position_id,
                event_type="position_manual_close_failed",
                level=EVENT_LEVEL_ERROR,
                message=f"Manual close failed: {exc}",
                payload={
                    "symbol": symbol,
                    "reason": reason,
                    "cancelled_conditional_orders": cancelled_orders,
                    "error": str(exc),
                },
                commit=True,
            )
            return AutoTradeFailedClosePositionInfo(
                position_id=position_id,
                symbol=symbol,
                error=str(exc),
            )

        # 4. Persist closure state and emit success event.
        self._mark_position_closed(
            position_row=position_row,
            close_reason=reason or "manual_close",
            close_price=avg_price,
        )
        await session.commit()
        self._untrack_position_in_ws_manager(position_row)
        await self._emit_event(
            session=session,
            user_id=int(position_row.user_id),
            config_id=int(position_row.config_id),
            profile_id=int(position_row.profile_id),
            history_id=position_row.close_history_id,
            position_id=position_id,
            event_type="position_manual_closed",
            level=EVENT_LEVEL_INFO,
            message="Position closed via manual close-positions endpoint.",
            payload={
                "symbol": symbol,
                "executed_qty": executed_qty,
                "avg_price": avg_price,
                "cancelled_conditional_orders": cancelled_orders,
                "reason": reason,
            },
            commit=True,
        )
        return AutoTradeClosedPositionInfo(
            position_id=position_id,
            symbol=symbol,
            side=cast(Any, side_raw),
            executed_qty=executed_qty,
            avg_price=avg_price,
            cancelled_conditional_orders=cancelled_orders,
        )

    @staticmethod
    def _iter_known_conditional_order_ids(
        position_row: AutoTradePosition,
    ) -> list[str]:
        order_ids: list[str] = []
        if position_row.sl_exchange_order_id:
            order_ids.append(str(position_row.sl_exchange_order_id))
        for level in position_row.tp_levels_json or []:
            if level.get("status") == "triggered":
                continue
            exchange_id = level.get("exchange_order_id")
            if exchange_id:
                order_ids.append(str(exchange_id))
        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for order_id in order_ids:
            if order_id in seen:
                continue
            seen.add(order_id)
            deduped.append(order_id)
        return deduped

    def _mark_position_closed(
        self,
        *,
        position_row: AutoTradePosition,
        close_reason: str,
        close_price: float | None = None,
    ) -> None:
        position_row.state = PositionState.CLOSED.value
        position_row.status = POSITION_CLOSED
        position_row.current_quantity = Decimal("0")
        position_row.closed_at = _utc_now()
        position_row.close_reason = close_reason
        if close_price is not None and close_price > 0:
            position_row.close_price = close_price

    @staticmethod
    def _untrack_position_in_ws_manager(position_row: AutoTradePosition) -> None:
        manager = _WS_MANAGER_REGISTRY.get(str(position_row.account_id))
        if manager is None:
            return
        try:
            manager.untrack_position(str(position_row.symbol))
        except Exception:
            logger.exception(
                "close_open_positions: untrack_position failed for %s on account %s",
                position_row.symbol,
                position_row.account_id,
            )

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
        config_id: int | None = None,
        closed_after: datetime | None = None,
        closed_before: datetime | None = None,
    ) -> list[AutoTradePosition]:
        # ``closed_after`` / ``closed_before`` narrow the result to positions
        # whose ``closed_at`` falls in ``[closed_after, closed_before)`` — an
        # inclusive lower / exclusive upper bound so a UTC-day window
        # ``[start_of_day, start_of_next_day)`` selects each closed trade
        # exactly once. Open positions (``closed_at IS NULL``) never satisfy a
        # bound, so they drop out whenever a window is supplied — that is the
        # intended behaviour for the daily-loss (T1.5) and health (T2.1)
        # callers, which always pair the window with ``status="closed"``.
        if account_id is None and config_id is None:
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
        if config_id is not None:
            stmt = stmt.where(AutoTradePosition.config_id == config_id)
        if status is not None:
            stmt = stmt.where(AutoTradePosition.status == status)
        if closed_after is not None:
            stmt = stmt.where(AutoTradePosition.closed_at >= closed_after)
        if closed_before is not None:
            stmt = stmt.where(AutoTradePosition.closed_at < closed_before)
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
        ledger_breakdown = await self._position_ledger_breakdown(
            session=session, position=position
        )
        breakdown_fields: dict[str, Any] = {
            "gross_realized_usdt": (
                ledger_breakdown.gross_realized if ledger_breakdown is not None else None
            ),
            "commission_usdt": (
                ledger_breakdown.commission if ledger_breakdown is not None else None
            ),
            "funding_usdt": (
                ledger_breakdown.funding if ledger_breakdown is not None else None
            ),
            "net_pnl_usdt": (
                ledger_breakdown.net_realized if ledger_breakdown is not None else None
            ),
        }
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
            if ledger_breakdown is not None:
                # Authoritative: Σ realized_pnl − commission + funding from the
                # synced ledger (multi-TP exact, funding included), not a single
                # close_price × quantity approximation.
                realized = ledger_breakdown.net_realized
            elif inferred is not None and inferred["realized_pnl_usdt"] is not None:
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
                **breakdown_fields,
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
        if ledger_breakdown is not None:
            # Realized booked by already-closed parts (multi-TP) + funding, from
            # the ledger — not just −fees, which silently dropped partial-close PnL.
            realized = ledger_breakdown.net_realized
        else:
            realized = -fees_usdt if unrealized is not None and fees_usdt else 0.0
        total = unrealized + realized if unrealized is not None else realized
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
            "realized_pnl_usdt": (
                realized if (unrealized is not None or ledger_breakdown is not None) else None
            ),
            "unrealized_pnl_usdt": unrealized,
            "total_pnl_usdt": total,
            **breakdown_fields,
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
        config_id: int | None = None,
        closed_after: datetime | None = None,
        closed_before: datetime | None = None,
    ) -> dict[str, Any]:
        positions = await self.list_positions(
            session=session,
            user_id=user_id,
            limit=limit,
            status=status,
            account_id=account_id,
            config_id=config_id,
            closed_after=closed_after,
            closed_before=closed_before,
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
        config_id: int | None = None,
    ) -> list[AutoTradeEvent]:
        stmt = select(AutoTradeEvent).where(AutoTradeEvent.user_id == user_id)
        if config_id is not None:
            # Direct config scoping — preferred under W7 multi-strategy when
            # two configs share an account but each shows its own log.
            stmt = stmt.where(AutoTradeEvent.config_id == config_id)
        elif account_id is not None:
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

    async def build_position_trace(
        self, *, session: AsyncSession, user_id: int, position_id: int
    ) -> tuple[AutoTradePosition, list[AutoTradeEvent]] | None:
        """Post-Trade execution trace (W9 — T3.1): the owning position plus all of
        its ``AutoTradeEvent`` rows in chronological (signal→close) order.

        Ownership-checked — returns ``None`` for an unknown or other-user position
        so the endpoint 404s (no cross-user leak). Read-only: no exchange calls and
        no core fetch — ``decision_event_id`` is surfaced as a pointer into core's
        ``ai_decision_events``, not dereferenced here.
        """
        position = await session.get(AutoTradePosition, position_id)
        if position is None or position.user_id != user_id:
            return None
        events = list(
            (
                await session.scalars(
                    select(AutoTradeEvent)
                    .where(AutoTradeEvent.position_id == position_id)
                    .order_by(AutoTradeEvent.created_at.asc(), AutoTradeEvent.id.asc())
                )
            ).all()
        )
        return position, events

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
        # P4-4 safety net: a non-live strategy must never place a *real-money*
        # order. On a demo account it trades normally (building the KPI sample);
        # on a real account it is blocked here (belt-and-suspenders to set_running,
        # e.g. a config that reached this path while non-live on a real account).
        if config.lifecycle_stage != LifecycleStage.LIVE.value and not await self._account_is_demo(
            session=session, account_id=config.account_id
        ):
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="signal_skipped_non_live",
                level=EVENT_LEVEL_INFO,
                message=(
                    f"Strategy not live (stage={config.lifecycle_stage}) on a real account; "
                    "no order placed."
                ),
                payload={"lifecycle_stage": config.lifecycle_stage},
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
                position_id=None,
                event_type="signal_neutral_no_action",
                level=EVENT_LEVEL_INFO,
                message="Neutral trend. No position opened.",
                payload={"trend": signal.trend, "confidence_pct": signal.confidence_pct},
                commit=False,
            )
            return

        # Defense-in-depth: even though the schema validator and the
        # backfill migration both reject fractional thresholds, a future
        # code path (manual DB edit, ORM-bypassing seed, schema drift on
        # downgrade) could re-introduce one. If we see ``min_confidence_pct``
        # land in (0, 1) at runtime we block the trade and emit a loud
        # audit event instead of silently letting every signal through.
        if 0 < config.min_confidence_pct < 1.0:
            state.last_trend = signal.trend
            state.opposite_streak = 0
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="config_min_confidence_pct_looks_like_fraction",
                level=EVENT_LEVEL_ERROR,
                message=(
                    "min_confidence_pct is below 1.0 — likely stored as a "
                    "fraction instead of a percent. Trades blocked until "
                    "the config is corrected."
                ),
                payload={
                    "min_confidence_pct": config.min_confidence_pct,
                    "fast_close_confidence_pct": config.fast_close_confidence_pct,
                    "signal_confidence_pct": signal.confidence_pct,
                    "trend": signal.trend,
                },
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

        # ---- T14 / W8b: data-freshness gate (acts on staleness, not just alerts) ----
        from app.core.freshness import age_minutes, normalize_to_utc
        from app.services.personal_analysis.freshness import should_block_stale_entry

        freshness_settings = get_settings()
        if freshness_settings.agent_freshness_block_enabled:
            now_ts = datetime.now(UTC)
            reference_at = normalize_to_utc(history.core_completed_at or history.created_at)
            if should_block_stale_entry(
                reference_at=reference_at,
                now=now_ts,
                threshold_minutes=freshness_settings.agent_freshness_threshold_minutes,
                enabled=True,
            ):
                age = age_minutes(reference_at, now=now_ts)
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=history.id,
                    position_id=None,
                    event_type="data_stale_blocked",
                    level=EVENT_LEVEL_WARNING,
                    message=(
                        "Entry blocked: AI data is stale "
                        f"(age {int(round(age)) if age is not None else 'n/a'} min > "
                        f"{freshness_settings.agent_freshness_threshold_minutes} min)."
                    ),
                    payload={
                        "trend": signal.trend,
                        "age_minutes": int(round(age)) if age is not None else None,
                        "threshold_minutes": freshness_settings.agent_freshness_threshold_minutes,
                    },
                    commit=False,
                )
                return

        # ---- W4 / Phases 1-2: AI Trend Overlay ----
        # Shared resolve: pull the freshest ai_trend from the local
        # personal-analysis cache exactly once so that every active phase
        # works off the same snapshot (avoids inconsistent decisions if a
        # new event were written between two reads). Missing/stale snapshot
        # triggers fail-open with a warn audit row, preserving pre-overlay
        # behaviour.
        overlay_config = AiOverlayConfig.from_record(config.ai_overlay_config_json)
        overlay_snapshot = None
        if overlay_config.enabled and (
            overlay_config.entry_side_lock_enabled
            or overlay_config.atr_scaling_enabled
            or overlay_config.rsi_scaling_enabled
        ):
            overlay_snapshot = await resolve_ai_trend(
                session=session,
                user_id=config.user_id,
                # ``history.symbol`` keeps the upstream signal symbol form
                # (e.g. ``BTCUSDT``) which is what ``PersonalAnalysisHistory``
                # stores. ``execution_symbol`` would be the exchange-normalised
                # ``BTC/USDT:USDT`` and would never match.
                symbol=history.symbol,
                max_age_minutes=overlay_config.stale_max_minutes,
                profile_id=config.profile_id,
            )
            if overlay_snapshot is None:
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=history.id,
                    position_id=None,
                    event_type=AiOverlayEventType.STALE_FALLBACK.value,
                    level=EVENT_LEVEL_INFO,
                    message="No fresh ai_trend available — overlay falls back to static params.",
                    payload=build_overlay_payload(
                        event_type=AiOverlayEventType.STALE_FALLBACK,
                        reason="no_fresh_ai_trend",
                        snapshot=None,
                        extra={
                            "phase": _overlay_active_phases_label(overlay_config),
                            "symbol": execution_symbol,
                        },
                    ),
                    commit=False,
                )

        # Phase 1: refuse entries that contradict a confident ai_trend.
        if (
            overlay_config.enabled
            and overlay_config.entry_side_lock_enabled
            and overlay_snapshot is not None
        ):
            intended_side = "long" if signal.trend == TREND_LONG else "short"
            block, reason = should_block_entry(intended_side, overlay_snapshot, overlay_config)
            if block:
                state.last_trend = signal.trend
                state.opposite_streak = 0
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=history.id,
                    position_id=None,
                    event_type=AiOverlayEventType.BLOCK_ENTRY.value,
                    level=EVENT_LEVEL_INFO,
                    message="Entry blocked by AI overlay (ai_trend opposes intended side).",
                    payload=build_overlay_payload(
                        event_type=AiOverlayEventType.BLOCK_ENTRY,
                        reason=reason,
                        snapshot=overlay_snapshot,
                        extra={
                            "intended_side": intended_side,
                            "signal_trend": signal.trend,
                            "signal_confidence_pct": signal.confidence_pct,
                        },
                    ),
                    commit=False,
                )
                return

        # ---- W8 / T1.2: Pre-Trade Risk Engine ----
        # Final gate before sizing/ordering, after the AI-overlay entry block.
        # A missing/disabled risk config is a no-op (fail-safe); a blocked
        # decision records a ``risk_blocked`` audit event and opens nothing.
        risk_decision = await self.evaluate_pre_trade_risk(
            session=session,
            config=config,
            signal=signal,
            execution_symbol=execution_symbol,
        )
        if not risk_decision.allowed:
            state.last_trend = signal.trend
            state.opposite_streak = 0
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="risk_blocked",
                level=EVENT_LEVEL_WARNING,
                message=risk_decision.reason,
                payload={"rule": risk_decision.rule, **risk_decision.payload},
                commit=False,
            )
            return
        if risk_decision.warning:
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=None,
                event_type="risk_check_degraded",
                level=EVENT_LEVEL_WARNING,
                message=risk_decision.warning,
                payload={"rule": "daily_loss_pct"},
                commit=False,
            )

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

        # W4 / Phase 2: AI Trend Overlay — ATR multiplier scaling.
        # Computed once at position-open time. We deliberately do NOT
        # re-evaluate it on every SL/TP tick to avoid flip-flopping the
        # stop loss when ai_trend updates mid-position.
        volatility_atr_override: float | None = None
        if (
            overlay_config.enabled
            and overlay_config.atr_scaling_enabled
            and overlay_snapshot is not None
        ):
            base_atr_mult = _resolve_base_atr_multiplier(config)
            position_side: PositionSideLiteral = "long" if signal.trend == TREND_LONG else "short"
            scaled_atr, atr_decision = scale_atr_multiplier(
                base_atr_mult, overlay_snapshot, overlay_config, position_side
            )
            if atr_decision.changed:
                volatility_atr_override = scaled_atr
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=history.id,
                    position_id=None,
                    event_type=AiOverlayEventType.ATR_SCALED.value,
                    level=EVENT_LEVEL_INFO,
                    message="ATR multiplier scaled by AI overlay.",
                    payload=build_overlay_payload(
                        event_type=AiOverlayEventType.ATR_SCALED,
                        reason=atr_decision.reason,
                        snapshot=overlay_snapshot,
                        before=base_atr_mult,
                        after=scaled_atr,
                        extra={"position_side": position_side},
                    ),
                    commit=False,
                )

        # W4 / Phase 3: AI Trend Overlay — RSI threshold shift.
        # The shift is symmetric (preserves the width of the neutral band)
        # and only applied to RSI watcher conditions at position-open time.
        rsi_condition_shift = 0
        if (
            overlay_config.enabled
            and overlay_config.rsi_scaling_enabled
            and overlay_snapshot is not None
        ):
            new_oversold, new_overbought, rsi_decision = scale_rsi_thresholds(
                30, 70, overlay_snapshot, overlay_config
            )
            if rsi_decision.changed:
                rsi_condition_shift = new_oversold - 30  # symmetric, equals overbought - 70
                await self._emit_event(
                    session=session,
                    user_id=config.user_id,
                    config_id=config.id,
                    profile_id=config.profile_id,
                    history_id=history.id,
                    position_id=None,
                    event_type=AiOverlayEventType.RSI_SCALED.value,
                    level=EVENT_LEVEL_INFO,
                    message="RSI watcher thresholds shifted by AI overlay.",
                    payload=build_overlay_payload(
                        event_type=AiOverlayEventType.RSI_SCALED,
                        reason=rsi_decision.reason,
                        snapshot=overlay_snapshot,
                        before=[30, 70],
                        after=[new_oversold, new_overbought],
                        extra={"shift": rsi_condition_shift},
                    ),
                    commit=False,
                )

        position_context = self._build_position_context(
            config=config,
            signal=signal,
            exchange_name=exchange_name,
            execution_symbol=execution_symbol,
            quantity=quantity,
            tp_price=tp_price,
            sl_price=sl_price,
            volatility_atr_multiplier_override=volatility_atr_override,
            rsi_condition_shift=rsi_condition_shift,
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
            # W2: persist the AI decision document id that drove this open
            # when overlay was active. Lets every position be traced back
            # to its exact AI decision in core regardless of whether
            # PersonalAnalysisHistory is later pruned.
            decision_event_id=(
                overlay_snapshot.decision_event_id if overlay_snapshot is not None else None
            ),
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
                # Surface the threshold that admitted this trade alongside
                # the signal confidence. Lets an operator spot a
                # mis-configured threshold (e.g. fractional 0.65) from the
                # audit feed alone — without it the "opened at 56 % when I
                # set 65 %" complaint takes a DB query to reproduce.
                "min_confidence_pct_at_open": float(config.min_confidence_pct),
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

        # Multi-TP ladder lock: once at least one TP level has fired, the
        # position's exit plan is committed to the TP ladder + dynamically
        # moving SL. Opposite-trend signals (fast-close OR streak-confirm)
        # would otherwise yank the remainder of the position at market
        # price — nullifying the user's intent of "let the lesenka run".
        # This is the gate the user explicitly requested after observing
        # TP1 firing and the rest being market-closed by a follow-up
        # signal. See README "Multi-TP ladder lock" for the semantics.
        #
        # When a single-TP position or a multi-TP position with no
        # triggered levels yet hits this branch, the legacy fast-close /
        # streak-close logic still applies — only an *engaged* ladder is
        # protected.
        if self._multi_tp_ladder_engaged(position):
            triggered_levels = self._triggered_tp_levels(position)
            await self._emit_event(
                session=session,
                user_id=config.user_id,
                config_id=config.id,
                profile_id=config.profile_id,
                history_id=history.id,
                position_id=position.id,
                event_type="opposite_signal_ignored_multi_tp_engaged",
                level=EVENT_LEVEL_INFO,
                message=(
                    "Opposite-trend signal ignored: multi-TP ladder is engaged "
                    "(exit managed by TP levels + dynamic SL)."
                ),
                payload={
                    "trend": signal.trend,
                    "confidence_pct": signal.confidence_pct,
                    "position_side": position.side,
                    "triggered_tp_levels": triggered_levels,
                    "tp_mode": position.tp_mode,
                },
                commit=False,
            )
            # Reset the streak so the next ladder run isn't biased by a
            # carry-over count, but keep last_trend updated so the audit
            # log accurately reflects what we observed.
            state.opposite_streak = 0
            state.last_trend = signal.trend
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
        # Fast-path KPI-Guard: a losing autonomous close may breach Max DD / daily
        # loss — pause now (same transaction) rather than waiting for the 5m cron.
        await self._maybe_auto_pause_after_close(session=session, config_id=config.id)

    @staticmethod
    def _is_position_closed_on_exchange(position: NormalizedFuturesPosition | None) -> bool:
        if position is None:
            return True
        return float(position.contracts) <= _POSITION_EPSILON

    @staticmethod
    def _triggered_tp_levels(position: AutoTradePosition) -> list[int]:
        """Return level indices (1-based) of TP levels currently triggered.

        Reads from the persisted ``tp_levels_json`` snapshot — the engine
        updates this in-place as TPs fire. Empty list for single-TP
        positions or multi-TP ladders that haven't fired any level yet.
        """
        levels = position.tp_levels_json or []
        triggered: list[int] = []
        for index, level in enumerate(levels, start=1):
            if isinstance(level, dict) and level.get("status") == "triggered":
                triggered.append(index)
        return triggered

    @classmethod
    def _multi_tp_ladder_engaged(cls, position: AutoTradePosition) -> bool:
        """True when the position's multi-TP ladder has at least one
        triggered level and is therefore actively managing the exit.

        Used to gate the fast-close / streak-confirm opposite-signal close
        path in ``_process_with_open_position``. Once the ladder is
        engaged, the remainder of the position is committed to the
        configured TP levels and dynamic SL — opposite-trend signals
        should not yank it at market price.
        """
        if position.tp_mode != "multi":
            return False
        return bool(cls._triggered_tp_levels(position))

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

    async def reconcile_open_positions_via_rest(self) -> dict[str, int]:
        """REST safety-net: detect positions closed on the exchange and sync DB.

        The Binance user-data WebSocket is unreliable on some real accounts — it
        can connect yet never deliver fill events, so an SL/TP fill goes
        unobserved and the position stays ``open`` in the DB long after the
        exchange closed it (observed: a real SL fill seen only ~44 min later via
        the signal-time sync). This loop polls every open position and, once the
        exchange *confirms* it is flat, marks the DB row closed and untracks it.
        Idempotent and replica-safe; never closes a position on a single
        possibly-transient flat read (uses ``_confirm_position_closed``).

        Returns counts ``{"checked", "closed", "errors"}``.
        """
        result = {"checked": 0, "closed": 0, "errors": 0}
        open_states = (
            PositionState.OPEN.value,
            PositionState.ADJUSTING.value,
            PositionState.CLOSING.value,
        )
        async with AsyncSessionFactory() as session:
            rows = (
                await session.execute(
                    select(
                        AutoTradePosition.id,
                        AutoTradePosition.account_id,
                        AutoTradePosition.user_id,
                        AutoTradePosition.symbol,
                    ).where(AutoTradePosition.state.in_(open_states))
                )
            ).all()

        for position_id, account_id, user_id, symbol in rows:
            result["checked"] += 1
            try:
                if await self._reconcile_single_position_via_rest(
                    position_id=int(position_id),
                    account_id=int(account_id),
                    user_id=int(user_id),
                    symbol=str(symbol),
                ):
                    result["closed"] += 1
            except Exception:
                result["errors"] += 1
                logger.exception(
                    "reconcile_open_positions_via_rest: failed for position_id=%s",
                    position_id,
                )
        return result

    async def _reconcile_single_position_via_rest(
        self,
        *,
        position_id: int,
        account_id: int,
        user_id: int,
        symbol: str,
    ) -> bool:
        """Close one position if the exchange confirms it is flat. Returns True if closed."""
        async with AsyncSessionFactory() as session:
            # Fast path: a single fetch. The common case (still open) costs one
            # request and returns immediately. Only a flat reading pays the
            # multi-attempt confirmation, which guards against a transient
            # false-flat from closing a live position.
            live = await self._trading.fetch_futures_position(
                session=session,
                user_id=user_id,
                account_id=account_id,
                symbol=symbol,
            )
            if not self._is_position_closed_on_exchange(live):
                # Position is still alive on the exchange, but an intermediate
                # multi-TP rung may have partially filled while the user-data WS
                # was silent — in which case the per-level SL shift (sl_lock_pct
                # / move_sl_to, the "After TP — SL move" rung) never ran. Mirror
                # the WS ``_handle_tp_triggered_event`` work from REST so
                # break-even / reduce-risk still applies on a dead stream.
                # Best-effort: a failure here must never break the closed-
                # detection contract (still returns False = "not closed").
                try:
                    await self._reconcile_partial_tp_via_rest(
                        position_id=position_id,
                        account_id=account_id,
                        user_id=user_id,
                        symbol=symbol,
                        live=live,
                    )
                except Exception:
                    logger.exception(
                        "reconcile: partial-TP SL reconcile failed for position_id=%s",
                        position_id,
                    )
                return False

            confirmed_flat, latest = await self._confirm_position_closed(
                session=session,
                user_id=user_id,
                account_id=account_id,
                symbol=symbol,
            )
            if not confirmed_flat:
                return False

            # Lock the row and re-check it is still open before closing, so two
            # replicas (or a racing signal-close) cannot double-close.
            stmt: Select[tuple[AutoTradePosition]] = select(AutoTradePosition).where(
                AutoTradePosition.id == position_id
            )
            stmt = self._with_for_update(session=session, stmt=stmt)
            row = cast(AutoTradePosition | None, await session.scalar(stmt))
            if (
                row is None
                or row.status == POSITION_CLOSED
                or row.state == PositionState.CLOSED.value
            ):
                return False

            close_price = (
                float(latest.mark_price)
                if latest is not None and latest.mark_price is not None
                else None
            )
            self._mark_position_closed(
                position_row=row,
                close_reason="reconciled_closed_on_exchange",
                close_price=close_price,
            )
            await self._emit_event(
                session=session,
                user_id=row.user_id,
                config_id=row.config_id,
                profile_id=row.profile_id,
                history_id=None,
                position_id=row.id,
                event_type="position_reconciled_closed_via_rest",
                level=EVENT_LEVEL_WARNING,
                message=(
                    "Position detected closed on exchange via REST reconcile "
                    "(user-data WebSocket missed the fill)."
                ),
                payload={"symbol": row.symbol},
                commit=False,
            )
            await session.commit()
            self._untrack_position_in_ws_manager(row)
            return True

    async def _reconcile_partial_tp_via_rest(
        self,
        *,
        position_id: int,
        account_id: int,
        user_id: int,
        symbol: str,
        live: NormalizedFuturesPosition | None,
    ) -> int:
        """Move the SL after a multi-TP rung filled while the user-data WS was silent.

        Companion to the full-close branch of ``_reconcile_single_position_via_rest``:
        that one fires when the exchange position is *flat*; this one fires when it
        is still *open* but has shrunk because an intermediate TP rung filled. On a
        healthy stream ``WebSocketManager._handle_tp_triggered_event`` observes the
        fill and runs ``MultiTPEngine.handle_tp_triggered`` to shift the SL per the
        rung's ``sl_lock_pct`` / ``move_sl_to`` directive (break-even, reduce risk,
        …). On the real accounts where the WS connects but never delivers fills,
        that never happens and the SL is left at its original price — exactly the
        observed "TP filled but SL didn't move" symptom. This is the REST-side
        defense-in-depth that performs the same SL shift.

        A rung is treated as filled when its exchange algo order (``algoId`` stored
        in ``TPLevel.exchange_order_id``) is no longer among the exchange's open
        algo orders. ``/fapi/v1/openAlgoOrders`` only lists ``NEW`` conditionals —
        a ``TRIGGERED`` (filled) order drops off it — so the disappearance is an
        unambiguous fill signal that, unlike a quantity-delta heuristic, never
        confuses a manual or external partial trade on the same symbol with a TP
        fill. A *cancelled* algo order also drops off the list, so the inference is
        corroborated against the live position size before any SL is moved. Only
        intermediate rungs are considered; the final rung flattens the position and
        is owned by the full-close branch.

        Idempotent and replica-safe: an advanced rung is persisted as ``triggered``
        and skipped on later passes. The position row is locked ``FOR UPDATE`` only
        for the read/decision and the final persist — the engine + order-queue work
        runs between them with NO DB lock held, to avoid an AB-BA deadlock against
        the SL-replace / WS-warmup paths that also take the order-queue lock.
        Returns the number of rungs whose SL shift was dispatched.
        """
        live_size = (
            abs(float(live.contracts))
            if live is not None and live.contracts is not None
            else 0.0
        )
        if live_size <= _POSITION_EPSILON:
            return 0

        # ── Phase 1: locked read + decision ──────────────────────────────────
        # The ``FOR UPDATE`` lock is held ONLY to read the row and decide which
        # rungs filled — NEVER while driving the engine / order queue below.
        # Holding the row lock across ``get_order_queue`` deadlocks (AB-BA): the
        # SL-replace enqueue and the WS warmup acquire the order-queue lock and
        # then need this very row, while we would hold the row and wait for the
        # order-queue lock. That froze the reconciler + WS warmup on the real
        # account. So the lock is dropped before any engine/queue work.
        async with AsyncSessionFactory() as session:
            stmt: Select[tuple[AutoTradePosition]] = select(AutoTradePosition).where(
                AutoTradePosition.id == position_id
            )
            stmt = self._with_for_update(session=session, stmt=stmt)
            row = cast(AutoTradePosition | None, await session.scalar(stmt))
            if (
                row is None
                or row.status != POSITION_OPEN
                or row.state == PositionState.CLOSED.value
                or str(row.tp_mode or "") != "multi"
            ):
                return 0

            payload = {
                column.name: getattr(row, column.name)
                for column in AutoTradePosition.__table__.columns
            }
            payload["position_id"] = str(row.id)
            try:
                ctx = PositionContext.from_db_row(payload)
            except Exception:
                logger.exception(
                    "_reconcile_partial_tp_via_rest: failed to build context for "
                    "position_id=%s",
                    position_id,
                )
                return 0

            # Candidate rungs: intermediate (non-final), placed on the exchange
            # (carry an algoId), and not yet advanced. The final rung is excluded —
            # when it fills the position goes flat and the full-close branch owns it.
            last_index = len(ctx.tp_levels) - 1
            candidates = [
                (index, level)
                for index, level in enumerate(ctx.tp_levels)
                if index != last_index
                and level.status in {"open", "pending"}
                and level.exchange_order_id
            ]
            if not candidates:
                return 0

            adapter = await self._create_exchange_adapter(session=session, position=ctx)
            try:
                open_orders = await adapter.get_open_conditional_orders(symbol)
            except Exception:
                logger.exception(
                    "_reconcile_partial_tp_via_rest: get_open_conditional_orders "
                    "failed for position_id=%s symbol=%s",
                    position_id,
                    symbol,
                )
                return 0
            open_algo_ids = {
                str(order.exchange_order_id)
                for order in open_orders
                if order.exchange_order_id
            }

            filled = [
                (index, level)
                for index, level in candidates
                if str(level.exchange_order_id) not in open_algo_ids
            ]
            if not filled:
                return 0

            # Corroborate the algo-status inference against the live position
            # size, tolerant of exchange LOT_SIZE rounding. Each rung's executed
            # quantity is the nominal ``original * close_pct`` snapped to the
            # symbol step (Binance LOT_SIZE: ``(qty - minQty) % stepSize == 0``),
            # so e.g. a 0.00175 rung on BTCUSDT (step 0.001) actually closes
            # 0.001. Summing raw nominals therefore overstates the close, and a
            # naive ``live == original - sum(nominal)`` check mis-fires on real
            # accounts (observed: position 274 looping size_mismatch every poll).
            #
            # Instead, accept inferred rungs in fill order only while the
            # *observed* close (``original - live``) covers at least half their
            # cumulative nominal — robust to step rounding (a truncated rung
            # always retains >50 % of its nominal) yet still rejecting a
            # disappeared-but-unfilled (cancelled) algo, where the position never
            # shrank and ``observed_closed`` stays ~0.
            original_quantity = float(ctx.original_quantity)
            observed_closed = max(original_quantity - live_size, 0.0)
            accepted: list[tuple[int, TPLevel]] = []
            cumulative_nominal = 0.0
            for index, level in filled:
                cumulative_nominal += original_quantity * (float(level.close_pct) / 100.0)
                if observed_closed + _POSITION_EPSILON >= cumulative_nominal * 0.5:
                    accepted.append((index, level))
                else:
                    break

            if not accepted:
                logger.warning(
                    "_reconcile_partial_tp_via_rest: live size %.10g for position_id=%s "
                    "did not shrink enough to confirm any of %d inferred TP fill(s) "
                    "(observed closed ~%.10g); skipping SL shift.",
                    live_size,
                    position_id,
                    len(filled),
                    observed_closed,
                )
                await self._emit_event(
                    session=session,
                    user_id=row.user_id,
                    config_id=row.config_id,
                    profile_id=row.profile_id,
                    history_id=None,
                    position_id=row.id,
                    event_type="multi_tp_rest_reconcile_size_mismatch",
                    level=EVENT_LEVEL_WARNING,
                    message=(
                        "REST reconcile saw a multi-TP algo order disappear but the "
                        "live position size did not shrink enough to confirm a fill; "
                        "SL shift skipped pending the next poll."
                    ),
                    payload={
                        "symbol": symbol,
                        "live_size": live_size,
                        "observed_closed": observed_closed,
                        "inferred_levels": [index for index, _level in filled],
                    },
                    commit=True,
                )
                return 0
        # ── row lock released ────────────────────────────────────────────────

        # ── Phase 2: drive the engine with NO DB lock held ───────────────────
        # ``get_order_queue`` and the SL-replace enqueue acquire the order-queue
        # lock, and the replace's ``on_success`` callback writes this row in its
        # own session — none of which may run while the Phase-1 lock is held.
        inferred_close_qty = sum(
            original_quantity * (float(level.close_pct) / 100.0)
            for _index, level in accepted
        )
        # Feed the engine the pre-fill quantity so its internal close-qty
        # subtraction keeps ``current_quantity`` positive through each dispatch;
        # the authoritative live size is pinned afterwards.
        ctx.current_quantity = live_size + inferred_close_qty

        queue = await get_order_queue(ctx)

        def _sl_callback_factory(
            triggered_level: int,
        ) -> Callable[[Any], Awaitable[None]]:
            return self._build_rest_sl_adjustment_callback(
                position=ctx,
                reason=WebSocketManager._tp_level_sl_adjustment_reason(ctx, triggered_level),
                trigger_source="rest_reconcile_inferred",
            )

        engine = MultiTPEngine(
            position=ctx,
            adapter=adapter,
            order_queue=queue,
            sl_adjustment_callback_factory=_sl_callback_factory,
        )

        advanced = 0
        for index, _level in sorted(accepted, key=lambda pair: pair[0]):
            try:
                await engine.handle_tp_triggered(triggered_level=index)
                advanced += 1
            except Exception:
                logger.exception(
                    "_reconcile_partial_tp_via_rest: handle_tp_triggered failed "
                    "for position_id=%s level=%s",
                    position_id,
                    index,
                )

        if advanced == 0:
            return 0

        # The exchange is authoritative for the live remaining; pin it so any
        # float drift from the per-level subtraction above cannot leak into the row.
        ctx.current_quantity = live_size

        # ── Phase 3: persist under a fresh short FOR UPDATE txn ───────────────
        # No engine/queue work inside this lock, so it cannot reintroduce the
        # AB-BA deadlock. Re-validate the row is still open before writing.
        async with AsyncSessionFactory() as session:
            stmt = self._with_for_update(
                session=session,
                stmt=select(AutoTradePosition).where(AutoTradePosition.id == position_id),
            )
            row = cast(AutoTradePosition | None, await session.scalar(stmt))
            if (
                row is None
                or row.status != POSITION_OPEN
                or row.state == PositionState.CLOSED.value
            ):
                # Position changed under us (e.g. fully closed); the SL shift was
                # already dispatched, nothing more to persist here.
                return advanced
            self._merge_position_context_into_row(row=row, position=ctx)
            await self._emit_event(
                session=session,
                user_id=row.user_id,
                config_id=row.config_id,
                profile_id=row.profile_id,
                history_id=None,
                position_id=row.id,
                event_type="multi_tp_reconciled_via_rest",
                level=EVENT_LEVEL_WARNING,
                message=(
                    "Multi-TP partial fill detected via REST reconcile (user-data "
                    "WebSocket missed the fill); SL shift dispatched."
                ),
                payload={
                    "symbol": symbol,
                    "advanced_levels": [index for index, _level in accepted],
                    "live_size": live_size,
                },
                commit=False,
            )
            await session.commit()
        return advanced

    def _build_rest_sl_adjustment_callback(
        self,
        *,
        position: PositionContext,
        reason: str,
        trigger_source: str,
    ) -> Callable[[Any], Awaitable[None]]:
        """SL-replace ``on_success`` hook for the REST partial-TP reconciler.

        Mirrors ``WebSocketManager._build_sl_adjustment_callback``: once the order
        queue confirms the replace, record the new SL price / order id, append an
        ``SLHistoryEntry``, complete the adjustment transition, and persist. Without
        it the SL moves on the exchange but the DB row keeps the stale price.
        """

        async def _callback(result: Any) -> None:
            if not isinstance(result, ConditionalOrderResult):
                return

            timestamp = datetime.now(UTC).isoformat()
            old_price = float(position.current_sl_price)
            new_price = (
                float(result.trigger_price)
                if float(result.trigger_price) > 0
                else old_price
            )

            position.current_sl_price = new_price
            position.sl_exchange_order_id = result.exchange_order_id
            position.last_adjusted_at = timestamp
            position.sl_history.append(
                SLHistoryEntry(
                    timestamp=timestamp,
                    old_price=old_price,
                    new_price=new_price,
                    reason=reason,
                    trigger_source=trigger_source,
                    exchange_order_id=result.exchange_order_id,
                )
            )

            if position.state_machine.can_transition(TransitionTrigger.ADJUSTMENT_COMPLETE):
                position.state = position.state_machine.transition(
                    TransitionTrigger.ADJUSTMENT_COMPLETE,
                    reason=f"SL adjustment applied via REST reconcile: {reason}",
                    metadata={
                        "source": trigger_source,
                        "exchange_order_id": result.exchange_order_id,
                        "new_sl_price": new_price,
                    },
                )
            else:
                position.state = position.state_machine.state

            await self._persist_runtime_position(position)

        return _callback

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
        # Queue the live push (B3) BEFORE committing so it fires from the after-commit
        # hook only if this row is actually durable — never before, never on rollback
        # (I1). Self-filters to streamable risk events.
        queue_user_event(
            session, user_id=user_id, event_type=event_type, payload=payload, message=message
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

    async def _position_ledger_breakdown(
        self,
        *,
        session: AsyncSession,
        position: AutoTradePosition,
    ) -> RealizedBreakdown | None:
        """Realized breakdown for a position from its synced ledger fills + funding.

        Returns ``None`` when no fills are linked to the position (e.g. not yet
        synced, or origin could not be resolved) so callers fall back to the
        legacy live-trade / directional path. Funding is attributed over the
        position's ``[opened_at, closed_at|now)`` window for its symbol.
        """
        rows = list(
            (
                await session.scalars(
                    select(ExchangeTradeLedger)
                    .where(
                        ExchangeTradeLedger.user_id == position.user_id,
                        ExchangeTradeLedger.auto_trade_position_id == position.id,
                    )
                    .order_by(ExchangeTradeLedger.traded_at)
                )
            ).all()
        )
        if not rows:
            return None
        opened_at = self._coerce_utc_datetime(position.opened_at)
        end = self._coerce_utc_datetime(position.closed_at) if position.closed_at else _utc_now()
        funding = await sum_funding(
            session=session,
            account_id=position.account_id,
            symbol=position.symbol,
            start=opened_at,
            end=end,
        )
        return compute_realized_breakdown(symbol=position.symbol, trades=rows, funding=funding)

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


# ─────────────────────────── lifespan integration ───────────────────────────


async def install_auto_trade_runtime(service: AutoTradeService) -> asyncio.Task[None]:
    """Wire process-wide audit + order_queue hooks and start the background runtime.

    Returns a single supervisor ``asyncio.Task`` (the reconciler + the in-position
    watcher-event consumer) so the caller (FastAPI lifespan) can cancel both cleanly
    on shutdown.
    """
    from app.services import audit as auto_trade_audit
    from app.services.position import order_queue as order_queue_mod

    auto_trade_audit.set_audit_hook(service._audit_emit_handler)  # noqa: SLF001

    async def _order_queue_audit_hook(
        task: order_queue_mod.OrderTask,
        error: Exception,
    ) -> None:
        # Provide a human-readable ``message`` alongside the structured
        # payload. Without it the Quick Log UI renders "-" for every fatal
        # error and operators have to dig into the JSON payload to diagnose.
        await auto_trade_audit.emit(
            "order_task_fatal_error",
            {
                "position_id": task.position_id,
                "action": task.action,
                "params": dict(task.params),
                "retry_count": task.retry_count,
                "error": str(error),
                "message": (
                    f"Order task {task.action!r} for position={task.position_id} "
                    f"failed after {task.retry_count} retries: {error}"
                ),
            },
        )

    order_queue_mod.set_fatal_error_audit_hook(_order_queue_audit_hook)

    # The safety audit hook surfaces non-fatal queue-side decisions (e.g.
    # ``emergency_close_skipped_position_flat``) through the same audit
    # plumbing as ``MultiTPEngine`` and the WS manager. Reusing
    # ``_audit_emit_handler`` keeps event payloads written to
    # ``auto_trade_events`` consistent across subsystems.
    order_queue_mod.set_safety_audit_hook(service._audit_emit_handler)  # noqa: SLF001

    await service.hydrate_active_positions()
    return asyncio.create_task(
        _auto_trade_runtime_main(service),
        name="auto_trade_runtime",
    )


async def _watcher_event_consumer_loop() -> None:
    """Consume in-position indicator-watcher events and act on them (T7 / W5b).

    The watcher worker computes RSI / MACD / EMA-cross inside an open position and
    publishes triggers to a Redis channel. Without a subscriber those triggers were
    dead in production — this loop routes each event to ``handle_watcher_event``
    (tighten-SL / partial-close / alert). It is resilient: a transient failure
    (e.g. a Redis blip) is logged and the subscription is retried after a short
    backoff. Cancellation propagates for clean shutdown.
    """
    from app.services.watchers.event_bus import handle_watcher_event, subscribe_watcher_events

    while True:
        try:
            await subscribe_watcher_events(handle_watcher_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "watcher event consumer crashed; resubscribing in %.0fs",
                _WATCHER_RESUBSCRIBE_DELAY_SECONDS,
            )
            await asyncio.sleep(_WATCHER_RESUBSCRIBE_DELAY_SECONDS)


async def _auto_trade_runtime_main(service: AutoTradeService) -> None:
    """Supervise the long-running auto-trade background tasks as one cancellable unit.

    Runs the periodic reconciler and the in-position watcher-event consumer
    concurrently; cancelling the returned task (on FastAPI shutdown) cancels both.
    """
    await asyncio.gather(
        _reconciler_loop(service),
        _watcher_event_consumer_loop(),
    )


async def _reconciler_loop(service: AutoTradeService) -> None:
    """Drive the two periodic safety passes.

    * Every ``_POSITION_RECONCILE_INTERVAL_SECONDS`` (~15s): poll the exchange
      for closed positions and sync DB state — the authoritative fill/closure
      detector for real accounts whose user-data WebSocket is silent.
    * Every ``_RECONCILER_INTERVAL_SECONDS`` (~60s): re-hydrate the WS-manager
      registry (catches positions opened by another replica / dropped tracking).

    Both are idempotent and replica-safe.
    """
    elapsed = 0.0
    while True:
        try:
            await asyncio.sleep(_POSITION_RECONCILE_INTERVAL_SECONDS)
            await service.reconcile_open_positions_via_rest()
            elapsed += _POSITION_RECONCILE_INTERVAL_SECONDS
            if elapsed >= _RECONCILER_INTERVAL_SECONDS:
                elapsed = 0.0
                await service.hydrate_active_positions()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("auto_trade reconciler tick failed")
