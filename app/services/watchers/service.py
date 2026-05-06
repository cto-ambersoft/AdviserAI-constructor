"""Watcher runtime services: DB loading, queue access, and Taskiq tick execution."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionFactory
from app.models.auto_trade_position import AutoTradePosition
from app.models.exchange import ExchangeCredential
from app.services.exchange.adapter import PositionSnapshot
from app.services.exchange.factory import ExchangeAdapterFactory
from app.services.exchange_credentials.service import ExchangeCredentialsService
from app.services.market_data.service import MarketDataService
from app.services.position.context import PositionContext, PositionSide, WatcherConfig
from app.services.position.order_queue import OrderExecutionQueue
from app.services.position.state_machine import PositionState
from app.services.watchers.indicator_watcher import IndicatorWatcher, WatcherEvent

logger = logging.getLogger(__name__)

_POSITION_COLUMNS = tuple(column.name for column in AutoTradePosition.__table__.columns)
_ACTIVE_WATCHER_STATES = frozenset({PositionState.OPEN, PositionState.ADJUSTING})
_TIMEFRAME_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[mhdw])$", re.IGNORECASE)
_ORDER_QUEUE_REGISTRY: dict[str, OrderExecutionQueue] = {}
_ORDER_QUEUE_RUNNERS: dict[str, asyncio.Task[None]] = {}
_ORDER_QUEUE_LOCK: asyncio.Lock | None = None
_WATCHER_MARKET_DATA = MarketDataService()


def _get_order_queue_lock() -> asyncio.Lock:
    global _ORDER_QUEUE_LOCK
    if _ORDER_QUEUE_LOCK is None:
        _ORDER_QUEUE_LOCK = asyncio.Lock()
    return _ORDER_QUEUE_LOCK


def _coerce_int_id(value: str | int, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return parsed


def _serialize_position_row(
    position_row: AutoTradePosition,
    *,
    exchange_name: str,
) -> dict[str, Any]:
    payload = {column: getattr(position_row, column) for column in _POSITION_COLUMNS}
    payload["position_id"] = str(position_row.id)
    payload["exchange"] = exchange_name
    return payload


async def load_position_context(
    position_id: str,
    *,
    session: AsyncSession | None = None,
) -> PositionContext:
    """Load a PositionContext for an auto-trade position id."""
    if session is None:
        async with AsyncSessionFactory() as managed_session:
            return await load_position_context(position_id, session=managed_session)

    stmt = (
        select(AutoTradePosition, ExchangeCredential.exchange_name)
        .join(ExchangeCredential, ExchangeCredential.id == AutoTradePosition.account_id)
        .where(AutoTradePosition.id == _coerce_int_id(position_id, field_name="position_id"))
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise LookupError(f"Position {position_id!r} was not found.")

    position_row, exchange_name = row
    return PositionContext.from_db_row(
        _serialize_position_row(position_row, exchange_name=str(exchange_name)),
    )


async def create_exchange_adapter_for_position(
    position: PositionContext,
    *,
    session: AsyncSession | None = None,
) -> Any:
    """Create an exchange adapter using the position's exchange credentials."""
    if session is None:
        async with AsyncSessionFactory() as managed_session:
            return await create_exchange_adapter_for_position(position, session=managed_session)

    credentials_service = ExchangeCredentialsService()
    credentials = await credentials_service.get_decrypted_credentials(
        session=session,
        account_id=_coerce_int_id(position.account_id, field_name="account_id"),
        user_id=_coerce_int_id(position.user_id, field_name="user_id"),
    )
    return await ExchangeAdapterFactory.create(
        exchange_name=credentials.exchange_name,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        mode=credentials.mode,
    )


async def get_order_queue(
    position: PositionContext,
    *,
    session: AsyncSession | None = None,
) -> OrderExecutionQueue:
    """Return a long-lived per-account order queue, creating it if needed."""
    account_key = str(position.account_id)
    lock = _get_order_queue_lock()

    async with lock:
        existing = _ORDER_QUEUE_REGISTRY.get(account_key)
        existing_runner = _ORDER_QUEUE_RUNNERS.get(account_key)
        if existing is not None and existing_runner is not None and not existing_runner.done():
            return existing

        adapter = await create_exchange_adapter_for_position(position, session=session)
        queue = OrderExecutionQueue(adapter=adapter, account_id=account_key)
        runner = asyncio.create_task(
            queue.start_processing(),
            name=f"order-queue-{account_key}",
        )
        runner.add_done_callback(_build_queue_runner_callback(account_key))

        _ORDER_QUEUE_REGISTRY[account_key] = queue
        _ORDER_QUEUE_RUNNERS[account_key] = runner
        return queue


def _build_queue_runner_callback(account_key: str) -> Callable[[asyncio.Task[None]], None]:
    def _on_done(task: asyncio.Task[None]) -> None:
        current_task = _ORDER_QUEUE_RUNNERS.get(account_key)
        if current_task is task:
            _ORDER_QUEUE_RUNNERS.pop(account_key, None)
            _ORDER_QUEUE_REGISTRY.pop(account_key, None)

        if task.cancelled():
            return

        try:
            exc = task.exception()
        except Exception:
            logger.exception(
                "Failed to inspect order queue runner task for account %s.", account_key
            )
            return

        if exc is not None:
            logger.error(
                "Order queue runner crashed for account %s.",
                account_key,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    return _on_done


def get_required_kline_limits(watchers: Sequence[WatcherConfig]) -> dict[str, int]:
    """Return the fetch limit per timeframe needed to evaluate active watchers."""
    requirements: dict[str, int] = {}
    for watcher in watchers:
        if not watcher.is_active:
            continue

        spec = IndicatorWatcher.INDICATOR_REGISTRY.get(watcher.indicator)
        if spec is None:
            continue

        timeframe = str(watcher.params.get("timeframe", "15m"))
        requirements[timeframe] = max(
            requirements.get(timeframe, 0),
            max(100, spec.min_bars(watcher.params)),
        )
    return requirements


def get_fastest_timeframe(watchers: Sequence[WatcherConfig]) -> str | None:
    """Return the shortest active watcher timeframe."""
    fastest: tuple[int, str] | None = None

    for watcher in watchers:
        if not watcher.is_active:
            continue

        timeframe = str(watcher.params.get("timeframe", "15m"))
        minutes = timeframe_to_minutes(timeframe)
        candidate = (minutes, timeframe)
        if fastest is None or candidate[0] < fastest[0]:
            fastest = candidate

    if fastest is None:
        return None
    return fastest[1]


def timeframe_to_minutes(timeframe: str) -> int:
    """Convert a ccxt timeframe string like 15m/1h to minutes."""
    match = _TIMEFRAME_RE.fullmatch(timeframe.strip())
    if match is None:
        raise ValueError(f"Unsupported timeframe format: {timeframe!r}")

    value = int(match.group("value"))
    unit = match.group("unit").lower()
    multipliers = {
        "m": 1,
        "h": 60,
        "d": 60 * 24,
        "w": 60 * 24 * 7,
    }
    return value * multipliers[unit]


def compute_tightened_sl(
    position: PositionContext,
    *,
    current_price: float,
    atr_value: float,
    offset_multiplier: float,
) -> float | None:
    """Compute a more protective SL based on current price and ATR distance."""
    if atr_value <= 0 or offset_multiplier <= 0:
        return None

    distance = atr_value * offset_multiplier
    if position.side == PositionSide.SHORT:
        candidate = current_price + distance
        if candidate >= position.current_sl_price:
            return None
        return candidate

    candidate = current_price - distance
    if candidate <= position.current_sl_price:
        return None
    return candidate


def extract_atr_value(position: PositionContext, event: WatcherEvent) -> float | None:
    """Resolve ATR from watcher payload, current indicator value, or position cache."""
    candidates = [
        event.action_params.get("current_atr"),
        event.current_value if event.indicator == "ATR" else None,
        position.volatility_last_atr,
    ]

    for candidate in candidates:
        if candidate is None:
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value

    return None


def resolve_current_price(position: PositionContext, snapshot: PositionSnapshot | None) -> float:
    """Pick the best available price for SL calculations."""
    if snapshot is not None and snapshot.mark_price > 0:
        return float(snapshot.mark_price)
    return float(position.entry_price)


async def send_watcher_notification(user_id: str, event: WatcherEvent) -> None:
    """Notification stub for watcher alerts."""
    logger.info(
        "Watcher alert for user=%s position=%s indicator=%s condition=%s value=%s",
        user_id,
        event.position_id,
        event.indicator,
        event.condition,
        event.current_value,
    )


def _normalize_kline_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    required_columns = ["open", "high", "low", "close", "volume"]
    if frame.empty:
        return pd.DataFrame(columns=required_columns)

    normalized = frame.loc[:, required_columns].copy()
    for column in required_columns:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    return normalized.dropna(subset=required_columns).reset_index(drop=True)


async def run_position_watcher_tick(position_id: str) -> dict[str, Any]:
    """Fetch klines, run watchers, and publish triggered watcher events."""
    from app.services.watchers.event_bus import publish_watcher_event

    async with AsyncSessionFactory() as session:
        try:
            position = await load_position_context(position_id, session=session)
        except (LookupError, ValueError):
            logger.warning("Watcher tick skipped because position %s was not found.", position_id)
            return {"position_id": position_id, "status": "missing_position", "events": 0}

        if position.state not in _ACTIVE_WATCHER_STATES:
            return {"position_id": position_id, "status": "inactive_state", "events": 0}

        requirements = get_required_kline_limits(position.active_watchers)
        if not requirements:
            return {"position_id": position_id, "status": "no_active_watchers", "events": 0}

        kline_buffers: dict[str, pd.DataFrame] = {}
        for timeframe, limit in requirements.items():
            market_frame = await _WATCHER_MARKET_DATA.fetch_ohlcv(
                exchange_name=position.exchange,
                symbol=position.symbol,
                timeframe=timeframe,
                bars=limit,
                market_type="futures",
            )
            kline_buffers[timeframe] = _normalize_kline_dataframe(market_frame)

        events = IndicatorWatcher(position).tick(kline_buffers)
        for event in events:
            await publish_watcher_event(event)

        return {"position_id": position_id, "status": "processed", "events": len(events)}
