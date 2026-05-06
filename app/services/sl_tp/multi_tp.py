"""Multi-TP execution engine."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any, Optional

from app.services.exchange.adapter import ExchangeAdapter, OrderSide
from app.services.position.context import PositionContext, PositionSide, TPHistoryEntry, TPLevel
from app.services.position.order_queue import OrderExecutionQueue, OrderPriority, OrderTask
from app.services.position.state_machine import PositionState, TransitionTrigger


class MultiTPEngine:
    """Manage TP level initialization and trigger handling."""

    def __init__(
        self,
        position: PositionContext,
        adapter: ExchangeAdapter,
        order_queue: OrderExecutionQueue,
        task_callback_factory: Any | None = None,
        sl_adjustment_callback_factory: Any | None = None,
    ) -> None:
        self.position = position
        self.adapter = adapter
        self.queue = order_queue
        self._task_callback_factory = task_callback_factory
        self._sl_adjustment_callback_factory = sl_adjustment_callback_factory

    async def initialize_tp_levels(self) -> None:
        """Enqueue placement tasks for all pending TP levels."""
        for index, level in enumerate(self.position.tp_levels):
            if level.status != "pending":
                continue

            task = OrderTask(
                priority=OrderPriority.NEW_CONDITIONAL,
                created_at=time.time(),
                position_id=self.position.position_id,
                action="place_tp",
                params={
                    "level": level.level,
                    "symbol": self.position.symbol,
                    "side": self._tp_order_side(),
                    "quantity": self.position.original_quantity * (level.close_pct / 100.0),
                    "trigger_price": level.trigger_price,
                    "client_order_id": self._build_client_order_id("tp", level_index=index),
                    "reduce_only": True,
                },
                on_success=self._build_task_callback(index, level),
            )
            await self.queue.enqueue(task)

    async def handle_tp_triggered(self, triggered_level: int) -> None:
        """Update position and queue follow-up actions after TP trigger."""
        level = self._resolve_level(triggered_level)
        close_qty = self.position.original_quantity * (level.close_pct / 100.0)

        new_quantity = self.position.current_quantity - close_qty
        if new_quantity < 0 and abs(new_quantity) <= 1e-9:
            new_quantity = 0.0
        self.position.current_quantity = max(new_quantity, 0.0)

        self.position.tp_history.append(
            TPHistoryEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                tp_level=triggered_level,
                old_price=level.trigger_price,
                new_price=level.trigger_price,
                reason=f"tp_level_{triggered_level + 1}_hit",
                close_pct=level.close_pct,
                exchange_order_id=level.exchange_order_id or "",
            )
        )

        level.status = "triggered"

        new_sl_price = self._resolve_sl_shift_target(level)
        if new_sl_price is not None and new_sl_price != self.position.current_sl_price:
            await self._enqueue_sl_replace(new_sl_price, triggered_level)

        remaining_levels = [
            item
            for item in self.position.tp_levels
            if item.status in {"pending", "open"}
        ]
        if not remaining_levels or self.position.current_quantity <= 0:
            self._transition_all_closed()
        else:
            self._transition_partial_close()

        self.position.state = self.position.state_machine.state

    def _resolve_level(self, triggered_level: int) -> TPLevel:
        if triggered_level < 0 or triggered_level >= len(self.position.tp_levels):
            raise IndexError(f"TP level index out of range: {triggered_level}")
        return self.position.tp_levels[triggered_level]

    def _resolve_sl_shift_target(self, level: TPLevel) -> Optional[float]:
        # Preferred numeric form: lock X% of profit on the entry→TP interval.
        # 0% = entry (breakeven), 100% = TP price, 50% = halfway, etc.
        # Works uniformly for LONG and SHORT — the interval (tp - entry) carries the sign.
        if level.sl_lock_pct is not None:
            entry = float(self.position.entry_price)
            tp_price = float(level.trigger_price)
            if entry <= 0 or tp_price <= 0:
                return None
            ratio = float(level.sl_lock_pct) / 100.0
            return entry + (tp_price - entry) * ratio

        # Legacy string form kept for back-compat.
        move_sl_to = level.move_sl_to
        if not isinstance(move_sl_to, str):
            return None

        normalized = move_sl_to.strip().lower()
        if normalized == "breakeven":
            return self.position.entry_price

        if normalized.startswith("tp"):
            raw_index = normalized.replace("tp", "", 1)
            if not raw_index.isdigit():
                return None

            referenced_level = int(raw_index) - 1
            if referenced_level < 0 or referenced_level >= len(self.position.tp_levels):
                return None

            return self.position.tp_levels[referenced_level].trigger_price

        return None

    async def _enqueue_sl_replace(self, new_sl_price: float, triggered_level: int) -> None:
        task = OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=time.time(),
            position_id=self.position.position_id,
            action="replace_sl",
            params={
                "symbol": self.position.symbol,
                "existing_order_id": self.position.sl_exchange_order_id or "active-sl",
                "new_trigger_price": new_sl_price,
                "trigger_price": new_sl_price,
                "new_quantity": self.position.current_quantity,
                "client_order_id": self._build_client_order_id("sl"),
                "reason": f"tp{triggered_level + 1}_hit_sl_adjustment",
            },
            on_success=self._build_sl_adjustment_callback(triggered_level),
        )
        await self.queue.enqueue(task)

    def _transition_partial_close(self) -> None:
        machine = self.position.state_machine
        reason = f"TP partial close, remaining={self.position.current_quantity:.8f}"

        if machine.can_transition(TransitionTrigger.PARTIAL_CLOSE):
            new_state = machine.transition(TransitionTrigger.PARTIAL_CLOSE, reason=reason)
            if (
                new_state == PositionState.CLOSING
                and machine.can_transition(TransitionTrigger.PARTIAL_CLOSE)
            ):
                machine.transition(TransitionTrigger.PARTIAL_CLOSE, reason=reason)
            return

        if machine.can_transition(TransitionTrigger.TP_TRIGGERED):
            machine.transition(TransitionTrigger.TP_TRIGGERED, reason=reason)
            if machine.can_transition(TransitionTrigger.PARTIAL_CLOSE):
                machine.transition(TransitionTrigger.PARTIAL_CLOSE, reason=reason)

    def _transition_all_closed(self) -> None:
        machine = self.position.state_machine
        reason = "All TP levels completed"

        if machine.can_transition(TransitionTrigger.ALL_CLOSED):
            machine.transition(TransitionTrigger.ALL_CLOSED, reason=reason)
            return

        if machine.can_transition(TransitionTrigger.TP_TRIGGERED):
            machine.transition(TransitionTrigger.TP_TRIGGERED, reason=reason)

        if machine.can_transition(TransitionTrigger.ALL_CLOSED):
            machine.transition(TransitionTrigger.ALL_CLOSED, reason=reason)
            return

        if machine.can_transition(TransitionTrigger.PARTIAL_CLOSE):
            machine.transition(TransitionTrigger.PARTIAL_CLOSE, reason=reason)
            if machine.can_transition(TransitionTrigger.ALL_CLOSED):
                machine.transition(TransitionTrigger.ALL_CLOSED, reason=reason)

    def _tp_order_side(self) -> OrderSide:
        if self.position.side == PositionSide.SHORT:
            return OrderSide.BUY
        return OrderSide.SELL

    def _build_client_order_id(self, kind: str, level_index: int | None = None) -> str:
        timestamp_ms = int(time.time() * 1000)
        suffix = f"-l{level_index + 1}" if level_index is not None else ""
        return f"{self.position.position_id}-{kind}{suffix}-{timestamp_ms}"

    def _build_task_callback(self, level_index: int, level: TPLevel) -> Any | None:
        if self._task_callback_factory is None:
            return None
        return self._task_callback_factory(level_index, level)

    def _build_sl_adjustment_callback(self, triggered_level: int) -> Any | None:
        if self._sl_adjustment_callback_factory is None:
            return None
        return self._sl_adjustment_callback_factory(triggered_level)
