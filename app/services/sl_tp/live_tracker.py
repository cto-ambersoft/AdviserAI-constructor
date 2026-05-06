"""Real-time SL adjustment driver fed by streaming kline ticks.

This module owns the per-symbol OHLCV buffer used by the SL adjustment pipeline
(trailing / breakeven / volatility). On every kline event delivered by the
exchange websocket, the buffer is updated and the pipeline is evaluated for
each tracked position whose strategy profile enabled any of those rules.

The class is deliberately decoupled from the WebSocket layer:
    * ``update_buffer`` and ``compute_atr`` are pure helpers.
    * ``on_tick`` accepts the live position list as an argument so that the
      caller (typically ``WebSocketManager``) controls which positions are
      currently active for this symbol.

Throttling and idempotency:
    * a per-position cooldown (``throttle_seconds``) prevents hammering the
      exchange with replace-SL requests on every tick;
    * the pipeline itself only returns adjustments that are *more protective*
      than the current SL, so identical ticks are no-ops.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pandas as pd
import pandas_ta as ta

from app.services.position.context import PositionContext
from app.services.position.order_queue import OrderExecutionQueue, OrderPriority, OrderTask
from app.services.position.state_machine import PositionState
from app.services.sl_tp.pipeline import SLAdjustmentPipeline

logger = logging.getLogger(__name__)


QueueResolver = Callable[[PositionContext], Awaitable[OrderExecutionQueue]]
ClientOrderIdFactory = Callable[[str, str], str]
PositionPersister = Callable[[PositionContext], Awaitable[None]]
TimeSource = Callable[[], float]


class RealtimeSLAdjuster:
    """Drive ``SLAdjustmentPipeline`` from a streaming kline feed for one symbol."""

    DEFAULT_BUFFER_BARS = 200
    DEFAULT_THROTTLE_SECONDS = 3.0

    def __init__(
        self,
        symbol: str,
        *,
        queue_resolver: QueueResolver,
        client_order_id_factory: ClientOrderIdFactory,
        persist_handler: PositionPersister,
        buffer_bars: int = DEFAULT_BUFFER_BARS,
        throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
        time_source: TimeSource = time.time,
    ) -> None:
        if buffer_bars <= 0:
            raise ValueError("buffer_bars must be positive")
        if throttle_seconds < 0:
            raise ValueError("throttle_seconds must be non-negative")
        self.symbol = symbol
        self._queue_resolver = queue_resolver
        self._client_order_id_factory = client_order_id_factory
        self._persist = persist_handler
        self._buffer_bars = buffer_bars
        self._throttle_seconds = float(throttle_seconds)
        self._now = time_source
        self._buffer: list[dict[str, Any]] = []
        self._last_adjustment_at: dict[str, float] = {}

    # ────────────────────────── public API ────────────────────────────────

    @staticmethod
    def needs_pipeline(position: PositionContext) -> bool:
        """Return True iff trailing, breakeven, or volatility-SL is enabled."""
        return bool(
            position.trailing_enabled
            or position.breakeven_enabled
            or position.volatility_sl_enabled
        )

    @property
    def buffer(self) -> list[dict[str, Any]]:
        """Read-only view of the internal kline buffer (mutating it is undefined)."""
        return self._buffer

    def update_buffer(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Append a closed bar or replace the in-progress bar in-place.

        Returns the canonical bar dict that ended up in the buffer, or None
        when the event payload is malformed.
        """
        bar = self._extract_bar(event)
        if bar is None:
            return None

        if self._buffer and self._buffer[-1].get("open_time") == bar["open_time"]:
            self._buffer[-1] = bar
        else:
            self._buffer.append(bar)

        overflow = len(self._buffer) - self._buffer_bars
        if overflow > 0:
            del self._buffer[:overflow]

        return bar

    def compute_atr(self, period: int) -> float | None:
        """Return the most recent ATR value or None if the buffer is too short."""
        if period <= 0 or len(self._buffer) < period + 1:
            return None
        df = pd.DataFrame(self._buffer)
        atr = ta.atr(df["high"], df["low"], df["close"], length=period)
        if atr is None or atr.empty:
            return None
        last = float(atr.iloc[-1])
        if math.isnan(last):
            return None
        return last

    def discard_position(self, position_id: str) -> None:
        """Drop throttle bookkeeping for a position that is no longer tracked."""
        self._last_adjustment_at.pop(position_id, None)

    async def on_tick(
        self,
        event: dict[str, Any],
        positions: list[PositionContext],
    ) -> list[str]:
        """Apply the SL pipeline to every relevant position and return ids adjusted."""
        bar = self.update_buffer(event)
        if bar is None:
            return []
        current_price = bar["close"]
        if current_price <= 0:
            return []

        adjusted: list[str] = []
        for position in positions:
            if not self._is_position_eligible(position):
                continue
            if not self._cooldown_elapsed(position.position_id):
                continue

            result = await self._evaluate_pipeline(position, current_price)
            if result is None:
                continue

            self._apply_state_changes(position, result)
            dispatched = await self._dispatch_replace_sl(position, result)
            if dispatched:
                adjusted.append(position.position_id)

        return adjusted

    # ────────────────────────── internals ────────────────────────────────

    def _is_position_eligible(self, position: PositionContext) -> bool:
        if position.symbol != self.symbol:
            return False
        if position.state != PositionState.OPEN:
            return False
        if not self.needs_pipeline(position):
            return False
        return True

    def _cooldown_elapsed(self, position_id: str) -> bool:
        last = self._last_adjustment_at.get(position_id, 0.0)
        return (self._now() - last) >= self._throttle_seconds

    async def _evaluate_pipeline(
        self,
        position: PositionContext,
        current_price: float,
    ) -> Any:
        indicators: dict[str, Any] = {}
        if position.volatility_sl_enabled:
            atr_value = self.compute_atr(int(position.volatility_atr_period))
            if atr_value is not None:
                indicators["ATR"] = atr_value

        pipeline = SLAdjustmentPipeline(position)
        return await pipeline.evaluate(
            current_price=current_price,
            indicators=indicators,
            kline_data=self._buffer,
        )

    def _apply_state_changes(
        self,
        position: PositionContext,
        result: Any,
    ) -> None:
        for field_name, value in result.update_tracking.items():
            if hasattr(position, field_name):
                setattr(position, field_name, value)
        position.current_sl_price = float(result.new_sl_price)
        self._last_adjustment_at[position.position_id] = self._now()

    async def _dispatch_replace_sl(
        self,
        position: PositionContext,
        result: Any,
    ) -> bool:
        if not position.sl_exchange_order_id:
            logger.warning(
                "Realtime SL pipeline result for position %s skipped: no active SL order id.",
                position.position_id,
            )
            return False
        if position.current_quantity <= 0:
            return False

        queue = await self._queue_resolver(position)
        await queue.enqueue(
            OrderTask(
                priority=OrderPriority.SL_ADJUSTMENT,
                created_at=self._now(),
                position_id=position.position_id,
                action="replace_sl",
                params={
                    "symbol": position.symbol,
                    "existing_order_id": position.sl_exchange_order_id,
                    "new_trigger_price": float(result.new_sl_price),
                    "trigger_price": float(result.new_sl_price),
                    "new_quantity": float(position.current_quantity),
                    "client_order_id": self._client_order_id_factory(
                        position.position_id, "rt-sl"
                    ),
                    "reason": f"realtime_pipeline:{result.reason}",
                },
            )
        )
        await self._persist(position)
        return True

    @staticmethod
    def _extract_bar(event: dict[str, Any]) -> dict[str, Any] | None:
        try:
            open_time = event.get("open_time", event.get("timestamp"))
            return {
                "open_time": int(open_time) if open_time is not None else 0,
                "open": float(event["open"]),
                "high": float(event["high"]),
                "low": float(event["low"]),
                "close": float(event["close"]),
                "volume": float(event.get("volume") or 0.0),
                "is_closed": bool(event.get("is_closed", False)),
            }
        except (KeyError, TypeError, ValueError):
            return None
