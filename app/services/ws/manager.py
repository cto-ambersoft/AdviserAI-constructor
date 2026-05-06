"""WebSocket manager per account."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.services.exchange.adapter import ConditionalOrderResult, ExchangeAdapter, OrderSide
from app.services.position.context import PositionContext, PositionSide, SLHistoryEntry
from app.services.position.order_queue import OrderPriority, OrderTask
from app.services.position.state_machine import (
    InvalidTransitionError,
    PositionState,
    TransitionTrigger,
)
from app.services.sl_tp.live_tracker import RealtimeSLAdjuster
from app.services.sl_tp.multi_tp import MultiTPEngine

logger = logging.getLogger(__name__)

PositionPersister = Callable[[PositionContext], Awaitable[Any] | Any]
OrderQueueResolver = Callable[[PositionContext], Awaitable[Any] | Any]


async def _maybe_await(result: Awaitable[Any] | Any) -> Any:
    """Await async results and return sync results unchanged."""
    if inspect.isawaitable(result):
        return await result
    return result


class WebSocketManager:
    """Manage a single account's private WebSocket lifecycle and guardrails."""

    WARMUP_SECONDS = 4.0
    STALE_TICK_THRESHOLD_PCT = 0.02
    JITTER_EMA_ALPHA = 0.3
    JITTER_UNHEALTHY_MS = 5000
    PROACTIVE_RECONNECT_COOLDOWN = 30
    SL_PIPELINE_KLINE_INTERVAL = "1m"

    def __init__(
        self,
        adapter: ExchangeAdapter,
        account_id: str,
        *,
        persist_position: PositionPersister | None = None,
        order_queue_resolver: OrderQueueResolver | None = None,
    ) -> None:
        self.adapter = adapter
        self.account_id = str(account_id)
        self._persist_position_handler = persist_position
        self._order_queue_resolver = order_queue_resolver

        self._connected = False
        self._positions: dict[str, PositionContext] = {}
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 50
        self._base_backoff = 1.0
        self._reconnecting = False
        self._reconnect_lock = asyncio.Lock()

        self._warmed_up = False
        self._warmup_started_at = 0.0
        self._last_prices: dict[str, float] = {}
        self._last_good_prices: dict[str, float] = {}
        self._last_heartbeat_at = 0.0
        self._last_heartbeat_gap_ms = 0.0
        self._jitter_ema_ms = 0.0
        self._last_proactive_reconnect_at = 0.0

        # Realtime SL pipeline (trailing/breakeven/volatility) — one tracker per symbol.
        self._sl_adjusters: dict[str, RealtimeSLAdjuster] = {}

    def track_position(self, position: PositionContext) -> None:
        """Register a live position for routing and reconciliation."""
        for alias in self._symbol_aliases(position.symbol):
            self._positions[alias] = position

    def untrack_position(self, symbol: str) -> None:
        """Remove a position from live routing."""
        position = self._find_position(symbol)
        if position is None:
            for alias in self._symbol_aliases(symbol):
                self._positions.pop(alias, None)
            return

        for alias in self._symbol_aliases(position.symbol):
            self._positions.pop(alias, None)
        self._cleanup_realtime_sl_pipeline(position)

    async def start(self) -> None:
        """Subscribe to the exchange user-data stream and reset warmup state."""
        self._warmed_up = False
        self._warmup_started_at = time.time()
        self._last_heartbeat_gap_ms = 0.0
        self._jitter_ema_ms = 0.0
        if not self._reconnecting:
            self._reconnect_attempts = 0

        await self.adapter.subscribe_user_data(
            on_order_update=self._handle_order_update,
            on_position_update=self._handle_position_update,
            on_disconnect=self._handle_disconnect,
        )

        self._connected = True
        self._last_heartbeat_at = time.time()
        logger.info(
            "[%s] WS connected, warmup started (%.1fs)",
            self.account_id,
            self.WARMUP_SECONDS,
        )

    def _check_warmup(self) -> bool:
        """Return True when the connection warmup window has elapsed."""
        if self._warmed_up:
            return True

        if self._warmup_started_at <= 0:
            return False

        elapsed = time.time() - self._warmup_started_at
        if elapsed >= self.WARMUP_SECONDS:
            self._warmed_up = True
            logger.info("[%s] WS warmup complete after %.1fs", self.account_id, elapsed)
            return True

        return False

    def _check_stale_tick(self, symbol: str, price: float) -> bool:
        """Reject anomalous price jumps while always refreshing the local cache."""
        normalized_price = float(price)
        last_price = self._last_prices.get(symbol)
        reference_price = self._last_good_prices.get(symbol, last_price)
        self._last_prices[symbol] = normalized_price

        if reference_price is not None and reference_price > 0:
            self._last_good_prices.setdefault(symbol, reference_price)

        if reference_price is None or reference_price <= 0 or normalized_price <= 0:
            self._last_good_prices[symbol] = normalized_price
            return True

        delta_pct = abs(normalized_price - reference_price) / abs(reference_price)
        threshold = self._get_stale_threshold(symbol)
        if delta_pct > threshold:
            logger.warning(
                "[%s] Stale tick rejected for %s: price=%s reference=%s last_seen=%s delta=%.4f threshold=%.4f",
                self.account_id,
                symbol,
                normalized_price,
                reference_price,
                last_price,
                delta_pct,
                threshold,
            )
            return False

        self._last_good_prices[symbol] = normalized_price
        return True

    def _get_stale_threshold(self, symbol: str) -> float:
        """Return a per-symbol stale-tick threshold."""
        normalized = symbol.upper().strip()
        if normalized.startswith("BTC") or normalized.startswith("ETH"):
            return self.STALE_TICK_THRESHOLD_PCT
        return 0.05

    def update_heartbeat(self) -> None:
        """Update heartbeat timing stats from an exchange pong/heartbeat event."""
        now = time.time()
        if self._last_heartbeat_at > 0:
            gap_ms = (now - self._last_heartbeat_at) * 1000.0
            self._last_heartbeat_gap_ms = gap_ms
            if self._jitter_ema_ms <= 0:
                self._jitter_ema_ms = gap_ms
            else:
                self._jitter_ema_ms = (
                    (self.JITTER_EMA_ALPHA * gap_ms)
                    + ((1.0 - self.JITTER_EMA_ALPHA) * self._jitter_ema_ms)
                )

        self._last_heartbeat_at = now

    async def _check_connection_health(self) -> None:
        """Trigger a proactive reconnect when the connection health degrades."""
        if not self._connected:
            return

        degraded_gap_ms = max(self._jitter_ema_ms, self._last_heartbeat_gap_ms)
        if degraded_gap_ms <= self.JITTER_UNHEALTHY_MS:
            return

        now = time.time()
        if now - self._last_proactive_reconnect_at < self.PROACTIVE_RECONNECT_COOLDOWN:
            return

        self._last_proactive_reconnect_at = now
        logger.warning(
            "[%s] Connection degraded: jitter_ema=%.0fms latest_gap=%.0fms threshold=%sms.",
            self.account_id,
            self._jitter_ema_ms,
            self._last_heartbeat_gap_ms,
            self.JITTER_UNHEALTHY_MS,
        )
        await self._handle_disconnect()

    async def _handle_order_update(self, event: dict[str, Any]) -> None:
        """Apply guard pipeline and forward safe order events to the position handler."""
        symbol = str(event.get("symbol", "") or "")
        price = self._extract_event_price(event)
        tick_valid = True

        if symbol and price > 0:
            tick_valid = self._check_stale_tick(symbol, price)

        if not self._check_warmup():
            logger.debug(
                "[%s] Warmup active, suppressing order update for %s.",
                self.account_id,
                symbol or "<unknown>",
            )
            return

        if not tick_valid:
            return

        position = self._find_position(symbol)
        if position is None:
            return

        await self._route_to_position(position, event)

    async def _handle_position_update(self, event: dict[str, Any]) -> None:
        """Always process confirmed position-state updates from the exchange."""
        symbol = str(event.get("symbol", "") or "")
        position = self._find_position(symbol)
        if position is None:
            return

        new_size = abs(self._coerce_float(event.get("size", event.get("qty")), default=0.0))
        if new_size <= 0:
            if position.state != PositionState.CLOSED or position.current_quantity > 0:
                position.current_quantity = 0.0
                await self._close_position(
                    position,
                    initial_trigger=TransitionTrigger.MANUAL_CLOSE,
                    reason=f"Position closed: {event.get('reason', 'unknown')}",
                    metadata=self._transition_metadata_from_event(event),
                )
                await self._cancel_remaining_orders(position)
                await self._persist_position(position)
            return

        if new_size != float(position.current_quantity):
            position.current_quantity = new_size
            await self._persist_position(position)

    async def _handle_disconnect(self) -> None:
        """Reconnect with exponential backoff and restore local state via REST sync."""
        if self._reconnecting:
            return

        async with self._reconnect_lock:
            if self._reconnecting:
                return

            self._reconnecting = True
            self._connected = False
            self._warmed_up = False

            try:
                for position in self._tracked_positions():
                    if position.state in {
                        PositionState.CLOSED,
                        PositionState.CANCELLED,
                        PositionState.FAILED,
                    }:
                        continue

                    await self._apply_transition(
                        position,
                        TransitionTrigger.WS_DISCONNECTED,
                        reason="WebSocket connection lost",
                    )
                    await self._persist_position(position)

                while self._reconnect_attempts < self._max_reconnect_attempts:
                    backoff = min(self._base_backoff * (2**self._reconnect_attempts), 60.0)
                    await asyncio.sleep(backoff)

                    attempt = self._reconnect_attempts + 1
                    self._reconnect_attempts = attempt

                    try:
                        # Drop stale tracker references — adapter will re-establish
                        # kline subscriptions through _ensure_realtime_sl_pipeline below.
                        self._sl_adjusters.clear()
                        await self.start()
                        await self._full_state_sync()
                        await self._restore_realtime_sl_pipelines()
                    except Exception as exc:
                        self._reconnect_attempts = attempt
                        logger.warning(
                            "[%s] Reconnect attempt %s failed: %s",
                            self.account_id,
                            attempt,
                            exc,
                        )
                    else:
                        self._reconnect_attempts = 0
                        return

                await self._emergency_close_all("Max WS reconnect attempts exceeded")
            finally:
                self._reconnecting = False

    async def _full_state_sync(self) -> None:
        """Reconcile local positions with exchange reality after reconnect."""
        for local_position in self._tracked_positions():
            await self._sync_position(local_position, reconnect_sync=True)

    async def _restore_realtime_sl_pipelines(self) -> None:
        """Re-subscribe kline streams for positions that need the realtime pipeline."""
        for position in self._tracked_positions():
            if position.state in {
                PositionState.CLOSED,
                PositionState.CANCELLED,
                PositionState.FAILED,
            }:
                continue
            await self._ensure_realtime_sl_pipeline(position)

    async def _route_to_position(self, position: PositionContext, event: dict[str, Any]) -> None:
        """Route lifecycle events first, then fall back to optional position handlers."""
        if await self._route_lifecycle_event(position, event):
            return

        for handler_name in ("handle_order_update", "on_order_update", "ws_order_handler"):
            handler = getattr(position, handler_name, None)
            if callable(handler):
                await _maybe_await(handler(event))
                return

        logger.debug(
            "[%s] No order-update handler is attached to position %s.",
            self.account_id,
            position.position_id,
        )

    async def _persist_position(self, position: PositionContext) -> None:
        """Persist the latest position state when an integration hook is configured."""
        if self._persist_position_handler is None:
            return
        await _maybe_await(self._persist_position_handler(position))

    async def _route_lifecycle_event(
        self,
        position: PositionContext,
        event: dict[str, Any],
    ) -> bool:
        if await self._handle_entry_fill_event(position, event):
            return True
        if await self._handle_sl_triggered_event(position, event):
            return True
        if await self._handle_tp_triggered_event(position, event):
            return True
        return False

    async def _handle_entry_fill_event(
        self,
        position: PositionContext,
        event: dict[str, Any],
    ) -> bool:
        if position.state != PositionState.ENTERING:
            return False
        if self._is_conditional_order_event(event) or self._is_reduce_only_event(event):
            return False
        if not self._is_fill_event(event):
            return False

        filled_quantity = self._resolve_filled_quantity(event, fallback=position.current_quantity)
        fill_price = self._resolve_fill_price(event, fallback=position.entry_price)
        if fill_price > 0:
            position.entry_price = fill_price
        if filled_quantity > 0:
            position.original_quantity = filled_quantity
            position.current_quantity = filled_quantity

        next_state = await self._apply_transition(
            position,
            TransitionTrigger.ENTRY_FILLED,
            reason="Entry order filled via WebSocket",
            metadata=self._transition_metadata_from_event(
                event,
                filled_quantity=filled_quantity,
                fill_price=fill_price,
            ),
        )

        if next_state == PositionState.OPEN:
            await self._enqueue_initial_protection_orders(position)
            await self._persist_position(position)
            await self._ensure_realtime_sl_pipeline(position)
        return True

    async def _handle_sl_triggered_event(
        self,
        position: PositionContext,
        event: dict[str, Any],
    ) -> bool:
        if not self._is_sl_trigger_event(position, event):
            return False
        if position.state == PositionState.CLOSED and position.current_quantity <= 0:
            return True

        reason = self._conditional_reason("Stop loss triggered", event)
        next_state = await self._apply_transition(
            position,
            TransitionTrigger.SL_TRIGGERED,
            reason=reason,
            metadata=self._transition_metadata_from_event(event),
        )
        if next_state != PositionState.CLOSING:
            return True

        position.current_quantity = 0.0
        self._append_sl_trigger_history(position, event, reason=reason)

        await self._apply_transition(
            position,
            TransitionTrigger.ALL_CLOSED,
            reason=reason,
            metadata=self._transition_metadata_from_event(event),
        )

        await self._cancel_remaining_orders(position)
        await self._persist_position(position)
        return True

    async def _handle_tp_triggered_event(
        self,
        position: PositionContext,
        event: dict[str, Any],
    ) -> bool:
        if not self._is_tp_trigger_event(position, event):
            return False

        if position.tp_mode == "multi" and position.tp_levels:
            matched_level = self._match_tp_level(position, event)
            if matched_level is None:
                return self._is_duplicate_tp_trigger(position, event)

            queue = await self._get_order_queue(position)
            engine = MultiTPEngine(
                position=position,
                adapter=self.adapter,
                order_queue=queue,
                sl_adjustment_callback_factory=lambda triggered_level: self._build_sl_adjustment_callback(
                    position=position,
                    reason=self._tp_level_sl_adjustment_reason(position, triggered_level),
                    trigger_source="multi_tp",
                ),
            )
            await engine.handle_tp_triggered(triggered_level=matched_level)
            if position.state == PositionState.CLOSED or position.current_quantity <= 0:
                await self._cancel_remaining_orders(position)
            await self._persist_position(position)
            return True

        if position.state == PositionState.CLOSED and position.current_quantity <= 0:
            return True

        reason = self._conditional_reason("Take profit triggered", event)
        position.current_quantity = 0.0
        next_state = await self._close_position(
            position,
            initial_trigger=TransitionTrigger.TP_TRIGGERED,
            reason=reason,
            metadata=self._transition_metadata_from_event(event),
        )
        if next_state is not None:
            await self._cancel_remaining_orders(position)
            await self._persist_position(position)
        return True

    async def _get_order_queue(self, position: PositionContext) -> Any:
        """Resolve the per-account order queue, using an injected resolver when available."""
        if self._order_queue_resolver is not None:
            return await _maybe_await(self._order_queue_resolver(position))

        from app.services.watchers.service import get_order_queue

        return await _maybe_await(get_order_queue(position))

    async def _enqueue_emergency_sl(self, position: PositionContext) -> None:
        """Re-place the stop loss immediately when it disappears after reconnect."""
        if position.current_quantity <= 0 or position.current_sl_price <= 0:
            logger.error(
                "[%s] Cannot enqueue emergency SL for %s: qty=%s sl=%s",
                self.account_id,
                position.symbol,
                position.current_quantity,
                position.current_sl_price,
            )
            return

        queue = await self._get_order_queue(position)
        await queue.enqueue(
            OrderTask(
                priority=OrderPriority.EMERGENCY_SL,
                created_at=time.time(),
                position_id=position.position_id,
                action="place_sl",
                params={
                    "symbol": position.symbol,
                    "side": self._closing_order_side(position.side),
                    "quantity": float(position.current_quantity),
                    "full_quantity": float(position.current_quantity),
                    "trigger_price": float(position.current_sl_price),
                    "client_order_id": self._build_client_order_id(position.position_id, "emergency-sl"),
                    "reduce_only": True,
                    "reason": "ws_full_state_sync_missing_sl",
                },
            )
        )

    async def _enqueue_initial_protection_orders(self, position: PositionContext) -> None:
        queue = await self._get_order_queue(position)

        if position.current_sl_price > 0:
            await queue.enqueue(
                OrderTask(
                    priority=OrderPriority.NEW_CONDITIONAL,
                    created_at=time.time(),
                    position_id=position.position_id,
                    action="place_sl",
                    params={
                        "symbol": position.symbol,
                        "side": self._closing_order_side(position.side),
                        "quantity": float(position.current_quantity),
                        "full_quantity": float(position.current_quantity),
                        "trigger_price": float(position.current_sl_price),
                        "client_order_id": self._build_client_order_id(
                            position.position_id,
                            "sl",
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
            for level_index, level in enumerate(position.tp_levels):
                if level.status != "pending":
                    continue

                await queue.enqueue(
                    OrderTask(
                        priority=OrderPriority.NEW_CONDITIONAL,
                        created_at=time.time(),
                        position_id=position.position_id,
                        action="place_tp",
                        params={
                            "level": level.level,
                            "symbol": position.symbol,
                            "side": self._closing_order_side(position.side),
                            "quantity": position.original_quantity * (level.close_pct / 100.0),
                            "trigger_price": level.trigger_price,
                            "client_order_id": self._build_client_order_id(
                                position.position_id,
                                f"tp-l{level_index + 1}",
                            ),
                            "reduce_only": True,
                        },
                        on_success=self._build_conditional_order_callback(
                            position=position,
                            source="tp",
                            level_index=level_index,
                        ),
                    )
                )
            return

        if position.current_tp_price is None or position.current_tp_price <= 0:
            return

        await queue.enqueue(
            OrderTask(
                priority=OrderPriority.NEW_CONDITIONAL,
                created_at=time.time(),
                position_id=position.position_id,
                action="place_tp",
                params={
                    "symbol": position.symbol,
                    "side": self._closing_order_side(position.side),
                    "quantity": float(position.current_quantity),
                    "trigger_price": float(position.current_tp_price),
                    "client_order_id": self._build_client_order_id(
                        position.position_id,
                        "tp",
                    ),
                    "reduce_only": True,
                },
            )
        )

    async def _ensure_realtime_sl_pipeline(self, position: PositionContext) -> None:
        """Subscribe a kline stream and create a tracker when the pipeline is enabled."""
        if not RealtimeSLAdjuster.needs_pipeline(position):
            return
        if position.symbol in self._sl_adjusters:
            return

        adjuster = RealtimeSLAdjuster(
            symbol=position.symbol,
            queue_resolver=self._get_order_queue,
            client_order_id_factory=self._build_client_order_id,
            persist_handler=self._persist_position,
        )
        self._sl_adjusters[position.symbol] = adjuster

        try:
            await self.adapter.subscribe_kline(
                symbol=position.symbol,
                interval=self.SL_PIPELINE_KLINE_INTERVAL,
                on_kline=self._build_realtime_kline_handler(position.symbol),
            )
        except Exception:
            self._sl_adjusters.pop(position.symbol, None)
            logger.exception(
                "[%s] Failed to subscribe kline %s/%s for realtime SL pipeline.",
                self.account_id,
                position.symbol,
                self.SL_PIPELINE_KLINE_INTERVAL,
            )

    def _build_realtime_kline_handler(
        self, symbol: str
    ) -> Callable[[dict[str, Any]], Awaitable[None]]:
        async def handler(event: dict[str, Any]) -> None:
            await self._handle_realtime_kline(symbol, event)

        return handler

    async def _handle_realtime_kline(self, symbol: str, event: dict[str, Any]) -> None:
        """Route an incoming kline tick to the matching SL adjuster."""
        adjuster = self._sl_adjusters.get(symbol)
        if adjuster is None:
            return

        positions = [
            position
            for position in self._tracked_positions()
            if position.symbol == symbol and RealtimeSLAdjuster.needs_pipeline(position)
        ]
        if not positions:
            return

        try:
            await adjuster.on_tick(event, positions)
        except Exception:
            logger.exception(
                "[%s] Realtime SL pipeline tick failed for %s.",
                self.account_id,
                symbol,
            )

    def _cleanup_realtime_sl_pipeline(self, closed_position: PositionContext) -> None:
        """Drop the per-position throttle entry and the symbol-level adjuster when idle."""
        adjuster = self._sl_adjusters.get(closed_position.symbol)
        if adjuster is None:
            return

        adjuster.discard_position(closed_position.position_id)

        for active in self._tracked_positions():
            if active.symbol != closed_position.symbol:
                continue
            if active.position_id == closed_position.position_id:
                continue
            if active.state in {
                PositionState.CLOSED,
                PositionState.CANCELLED,
                PositionState.FAILED,
            }:
                continue
            if RealtimeSLAdjuster.needs_pipeline(active):
                return

        # No other tracked position needs this adjuster; drop the local reference.
        # The kline subscription task is owned by the adapter and will be torn down
        # together with the WebSocketManager (or reset on reconnect).
        self._sl_adjusters.pop(closed_position.symbol, None)

    async def _emergency_close_all(self, reason: str) -> None:
        """Best-effort emergency market close if reconnect recovery is exhausted."""
        for position in self._tracked_positions():
            if position.current_quantity <= 0 or position.state == PositionState.CLOSED:
                continue

            queue = await self._get_order_queue(position)
            await queue.enqueue(
                OrderTask(
                    priority=OrderPriority.EMERGENCY_CLOSE,
                    created_at=time.time(),
                    position_id=position.position_id,
                    action="emergency_market_close",
                    params={
                        "symbol": position.symbol,
                        "side": self._closing_order_side(position.side),
                        "full_quantity": float(position.current_quantity),
                        "client_order_id": self._build_client_order_id(
                            position.position_id,
                            "emergency-close",
                        ),
                        "reason": reason,
                    },
                )
            )

    async def _apply_transition(
        self,
        position: PositionContext,
        trigger: TransitionTrigger,
        *,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> PositionState | None:
        try:
            next_state = position.state_machine.transition(
                trigger,
                reason=reason,
                metadata=metadata,
            )
        except InvalidTransitionError as exc:
            logger.error(
                "[%s] Invalid transition %s for position %s in state %s: %s",
                self.account_id,
                trigger.value,
                position.position_id,
                position.state.value,
                exc,
            )
            await self._attempt_state_sync(position, reason=str(exc))
            return None

        position.state = next_state
        return next_state

    async def _close_position(
        self,
        position: PositionContext,
        *,
        initial_trigger: TransitionTrigger,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> PositionState | None:
        if position.state_machine.can_transition(initial_trigger):
            await self._apply_transition(
                position,
                initial_trigger,
                reason=reason,
                metadata=metadata,
            )

        if position.state_machine.can_transition(TransitionTrigger.ALL_CLOSED):
            return await self._apply_transition(
                position,
                TransitionTrigger.ALL_CLOSED,
                reason=reason,
                metadata=metadata,
            )

        return await self._apply_transition(
            position,
            TransitionTrigger.ALL_CLOSED,
            reason=reason,
            metadata=metadata,
        )

    async def _attempt_state_sync(self, position: PositionContext, *, reason: str) -> None:
        try:
            await self._sync_position(position, reconnect_sync=False, sync_reason=reason)
        except Exception:
            logger.exception(
                "[%s] State sync failed for position %s after invalid transition.",
                self.account_id,
                position.position_id,
            )

    async def _sync_position(
        self,
        position: PositionContext,
        *,
        reconnect_sync: bool,
        sync_reason: str | None = None,
    ) -> None:
        exchange_position = await self.adapter.get_position(position.symbol)

        if exchange_position is None or abs(float(exchange_position.size)) == 0:
            position.current_quantity = 0.0
            self._apply_sync_transition(
                position,
                TransitionTrigger.ALL_CLOSED,
                target_state=PositionState.CLOSED,
                reason=sync_reason or "Position closed during state sync",
            )
            await self._persist_position(position)
            return

        synced_size = abs(float(exchange_position.size))
        if synced_size != float(position.current_quantity):
            position.current_quantity = synced_size

        if float(exchange_position.mark_price) > 0:
            normalized_symbol = self._normalize_symbol_key(position.symbol)
            if normalized_symbol:
                self._last_prices[normalized_symbol] = float(exchange_position.mark_price)
                self._last_good_prices[normalized_symbol] = float(exchange_position.mark_price)

        if position.state in {
            PositionState.PENDING,
            PositionState.ENTERING,
            PositionState.CLOSED,
            PositionState.CANCELLED,
            PositionState.FAILED,
        }:
            logger.warning(
                "[%s] Sync forcing position %s back to open with live size %.8f.",
                self.account_id,
                position.position_id,
                synced_size,
            )
            position.state = PositionState.OPEN
            position.state_machine.state = PositionState.OPEN

        if reconnect_sync:
            open_orders = await self.adapter.get_open_conditional_orders(position.symbol)
            sl_exists = any(str(order.order_type).lower() == "stop_loss" for order in open_orders)

            if not sl_exists and position.state != PositionState.CLOSED:
                logger.critical(
                    "[%s] Position %s lost its SL while disconnected; enqueueing emergency replacement.",
                    self.account_id,
                    position.symbol,
                )
                await self._enqueue_emergency_sl(position)

            if position.state == PositionState.RECONNECTING:
                self._apply_sync_transition(
                    position,
                    TransitionTrigger.SYNC_COMPLETE,
                    target_state=PositionState.OPEN,
                    reason=sync_reason or "Full state sync after reconnection",
                )

        await self._persist_position(position)

    def _apply_sync_transition(
        self,
        position: PositionContext,
        trigger: TransitionTrigger,
        *,
        target_state: PositionState,
        reason: str,
    ) -> PositionState:
        try:
            next_state = position.state_machine.transition(
                trigger,
                reason=reason,
                metadata={"source": "ws_state_sync"},
            )
        except InvalidTransitionError:
            logger.warning(
                "[%s] Sync forcing position %s to %s after failed %s transition.",
                self.account_id,
                position.position_id,
                target_state.value,
                trigger.value,
            )
            next_state = target_state
            position.state_machine.state = target_state

        position.state = next_state
        return next_state

    async def _cancel_remaining_orders(self, position: PositionContext) -> None:
        order_ids = {
            order_id
            for order_id in (
                self._known_conditional_order_ids(position)
                | set(await self._fetch_open_conditional_order_ids(position))
            )
            if order_id
        }
        if not order_ids:
            return

        for order_id in order_ids:
            try:
                await self.adapter.cancel_conditional_order(position.symbol, order_id)
            except Exception:
                logger.exception(
                    "[%s] Failed to cancel conditional order %s for closed position %s.",
                    self.account_id,
                    order_id,
                    position.position_id,
                )

        if position.sl_exchange_order_id in order_ids:
            position.sl_exchange_order_id = None
        for level in position.tp_levels:
            if level.exchange_order_id in order_ids and level.status != "triggered":
                level.status = "cancelled"

    async def _fetch_open_conditional_order_ids(self, position: PositionContext) -> set[str]:
        try:
            open_orders = await self.adapter.get_open_conditional_orders(position.symbol)
        except Exception:
            logger.exception(
                "[%s] Failed to fetch conditional orders for %s during close cleanup.",
                self.account_id,
                position.position_id,
            )
            return set()

        return {
            str(order.exchange_order_id)
            for order in open_orders
            if str(order.exchange_order_id)
        }

    def _known_conditional_order_ids(self, position: PositionContext) -> set[str]:
        order_ids = set()
        if position.sl_exchange_order_id:
            order_ids.add(str(position.sl_exchange_order_id))
        for level in position.tp_levels:
            if level.exchange_order_id and level.status != "triggered":
                order_ids.add(str(level.exchange_order_id))
        return order_ids

    def _build_conditional_order_callback(
        self,
        *,
        position: PositionContext,
        source: str,
        level_index: int | None = None,
    ) -> Callable[[Any], Awaitable[None]]:
        async def _callback(result: Any) -> None:
            if not isinstance(result, ConditionalOrderResult):
                return

            if source == "sl":
                position.sl_exchange_order_id = result.exchange_order_id
            elif source == "tp" and level_index is not None and 0 <= level_index < len(position.tp_levels):
                position.tp_levels[level_index].exchange_order_id = result.exchange_order_id
                position.tp_levels[level_index].status = "open"

            await self._persist_position(position)

        return _callback

    def _build_sl_adjustment_callback(
        self,
        *,
        position: PositionContext,
        reason: str,
        trigger_source: str,
        update_tracking: dict[str, float | bool] | None = None,
    ) -> Callable[[Any], Awaitable[None]]:
        async def _callback(result: Any) -> None:
            if not isinstance(result, ConditionalOrderResult):
                return

            timestamp = datetime.now(UTC).isoformat()
            old_price = float(position.current_sl_price)
            new_price = float(result.trigger_price) if float(result.trigger_price) > 0 else old_price

            position.current_sl_price = new_price
            position.sl_exchange_order_id = result.exchange_order_id
            position.last_adjusted_at = timestamp

            if update_tracking:
                for attribute, value in update_tracking.items():
                    if hasattr(position, attribute):
                        setattr(position, attribute, value)

            if reason in {"trailing", "breakeven", "volatility"}:
                position.sl_type = reason

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
                    reason=f"SL adjustment applied: {reason}",
                    metadata={
                        "source": trigger_source,
                        "exchange_order_id": result.exchange_order_id,
                        "new_sl_price": new_price,
                    },
                )
            else:
                position.state = position.state_machine.state

            await self._persist_position(position)

        return _callback

    def _tracked_positions(self) -> list[PositionContext]:
        unique: dict[str, PositionContext] = {}
        for position in self._positions.values():
            key = position.position_id or f"obj:{id(position)}"
            unique.setdefault(key, position)
        return list(unique.values())

    def _find_position(self, symbol: str) -> PositionContext | None:
        for alias in self._symbol_aliases(symbol):
            position = self._positions.get(alias)
            if position is not None:
                return position
        return None

    @classmethod
    def _symbol_aliases(cls, symbol: str) -> tuple[str, ...]:
        raw = str(symbol or "").strip()
        if not raw:
            return ()

        normalized = cls._normalize_symbol_key(raw)
        if normalized == raw:
            return (raw,)
        return (raw, normalized)

    @staticmethod
    def _normalize_symbol_key(symbol: str) -> str:
        raw = str(symbol or "").strip()
        if not raw:
            return ""
        return raw.split(":", 1)[0].replace("/", "").replace("-", "").replace("_", "").upper()

    @staticmethod
    def _order_type(event: dict[str, Any]) -> str:
        return str(event.get("order_type", "")).strip().lower()

    @staticmethod
    def _order_status(event: dict[str, Any]) -> str:
        return str(event.get("status", "")).strip().lower()

    @staticmethod
    def _execution_type(event: dict[str, Any]) -> str:
        return str(event.get("execution_type", "")).strip().lower()

    def _is_conditional_order_event(self, event: dict[str, Any]) -> bool:
        return self._order_type(event) in {"stop_loss", "take_profit", "trailing_stop"} or bool(
            event.get("is_algo")
        )

    @staticmethod
    def _is_reduce_only_event(event: dict[str, Any]) -> bool:
        return bool(event.get("reduce_only") or event.get("close_position") or event.get("close_on_trigger"))

    def _is_fill_event(self, event: dict[str, Any]) -> bool:
        if self._order_status(event) in {"filled", "triggered"}:
            return True
        if self._execution_type(event) in {"trade", "filled"}:
            return True
        if self._coerce_float(event.get("last_filled_quantity"), default=0.0) > 0:
            return True
        return self._coerce_float(event.get("filled_quantity"), default=0.0) > 0

    def _is_sl_trigger_event(self, position: PositionContext, event: dict[str, Any]) -> bool:
        if not self._is_fill_event(event):
            return False

        order_type = self._order_type(event)
        if order_type in {"stop_loss", "trailing_stop"}:
            return True

        event_order_id = self._event_order_identifier(event)
        if not event_order_id or not position.sl_exchange_order_id:
            return False
        return event_order_id == str(position.sl_exchange_order_id)

    def _is_tp_trigger_event(self, position: PositionContext, event: dict[str, Any]) -> bool:
        if not self._is_fill_event(event):
            return False

        if self._order_type(event) == "take_profit":
            return True

        event_order_id = self._event_order_identifier(event)
        if not event_order_id:
            return False

        return any(
            level.exchange_order_id and str(level.exchange_order_id) == event_order_id
            for level in position.tp_levels
        )

    def _match_tp_level(self, position: PositionContext, event: dict[str, Any]) -> int | None:
        event_order_id = self._event_order_identifier(event)
        if event_order_id:
            for index, level in enumerate(position.tp_levels):
                if level.status == "triggered":
                    continue
                if level.exchange_order_id and str(level.exchange_order_id) == event_order_id:
                    return index

        event_price = self._extract_event_price(event)
        if event_price <= 0:
            return None

        for index, level in enumerate(position.tp_levels):
            if level.status == "triggered":
                continue
            tolerance = max(abs(level.trigger_price) * 1e-6, 1e-8)
            if abs(level.trigger_price - event_price) <= tolerance:
                return index

        return None

    def _is_duplicate_tp_trigger(self, position: PositionContext, event: dict[str, Any]) -> bool:
        event_order_id = self._event_order_identifier(event)
        if event_order_id:
            return any(
                level.status == "triggered"
                and level.exchange_order_id
                and str(level.exchange_order_id) == event_order_id
                for level in position.tp_levels
            )

        event_price = self._extract_event_price(event)
        if event_price <= 0:
            return False

        for level in position.tp_levels:
            if level.status != "triggered":
                continue
            tolerance = max(abs(level.trigger_price) * 1e-6, 1e-8)
            if abs(level.trigger_price - event_price) <= tolerance:
                return True
        return False

    @staticmethod
    def _event_order_identifier(event: dict[str, Any]) -> str:
        for key in ("order_id", "client_order_id"):
            value = str(event.get(key, "")).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _tp_level_sl_adjustment_reason(
        position: PositionContext,
        triggered_level: int,
    ) -> str:
        if triggered_level < 0 or triggered_level >= len(position.tp_levels):
            return "multi_tp"

        move_sl_to = position.tp_levels[triggered_level].move_sl_to
        if not isinstance(move_sl_to, str):
            return "multi_tp"

        normalized = move_sl_to.strip().lower()
        if normalized == "breakeven":
            return "breakeven"
        return f"multi_tp:{normalized}"

    def _resolve_filled_quantity(self, event: dict[str, Any], *, fallback: float) -> float:
        for key in ("filled_quantity", "last_filled_quantity", "quantity", "order_quantity"):
            value = self._coerce_float(event.get(key), default=0.0)
            if value > 0:
                return value
        return max(float(fallback), 0.0)

    def _resolve_fill_price(self, event: dict[str, Any], *, fallback: float) -> float:
        for key in ("average_price", "last_fill_price", "price", "trigger_price", "mark_price"):
            value = self._coerce_float(event.get(key), default=0.0)
            if value > 0:
                return value
        return max(float(fallback), 0.0)

    def _append_sl_trigger_history(
        self,
        position: PositionContext,
        event: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        trigger_price = self._coerce_float(event.get("trigger_price"), default=0.0)
        if trigger_price <= 0:
            trigger_price = position.current_sl_price

        position.sl_history.append(
            SLHistoryEntry(
                timestamp=self._event_timestamp(event),
                old_price=float(position.current_sl_price),
                new_price=float(trigger_price),
                reason=reason,
                trigger_source="ws_manager",
                exchange_order_id=str(event.get("order_id") or position.sl_exchange_order_id or ""),
            )
        )

    def _conditional_reason(self, prefix: str, event: dict[str, Any]) -> str:
        order_id = str(event.get("order_id", "") or event.get("client_order_id", "")).strip()
        if order_id:
            return f"{prefix}: {order_id}"
        return prefix

    def _transition_metadata_from_event(
        self,
        event: dict[str, Any],
        **extra: object,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "event_type": event.get("event_type", event.get("type")),
            "order_id": event.get("order_id"),
            "client_order_id": event.get("client_order_id"),
            "status": event.get("status"),
            "order_type": event.get("order_type"),
            "symbol": event.get("symbol"),
            "transaction_time": event.get("transaction_time"),
        }
        metadata.update(extra)
        return {key: value for key, value in metadata.items() if value not in (None, "")}

    def _event_timestamp(self, event: dict[str, Any]) -> str:
        for key in ("transaction_time", "event_time"):
            raw_value = event.get(key)
            if raw_value in (None, ""):
                continue
            try:
                timestamp = float(raw_value)
            except (TypeError, ValueError):
                continue

            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()

        return datetime.now(UTC).isoformat()

    @staticmethod
    def _coerce_float(value: object, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _extract_event_price(event: dict[str, Any]) -> float:
        for key in ("price", "trigger_price", "mark_price", "last_price"):
            value = event.get(key)
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return 0.0

    @staticmethod
    def _closing_order_side(side: PositionSide) -> OrderSide:
        if side == PositionSide.SHORT:
            return OrderSide.BUY
        return OrderSide.SELL

    def _build_client_order_id(self, position_id: str, action: str) -> str:
        return f"{self.account_id}-{position_id}-{action}-{int(time.time() * 1000)}"
