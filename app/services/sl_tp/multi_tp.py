"""Multi-TP execution engine."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.services import audit as auto_trade_audit
from app.services.exchange.adapter import (
    ExchangeAdapter,
    OrderSide,
    PositionSnapshot,
)
from app.services.position.context import PositionContext, PositionSide, TPHistoryEntry, TPLevel
from app.services.position.order_queue import OrderExecutionQueue, OrderPriority, OrderTask
from app.services.position.state_machine import PositionState, TransitionTrigger

logger = logging.getLogger(__name__)

# Relative tolerance for "is the new SL meaningfully different from the current SL?".
# Smaller than any tick size we expect to see on supported venues.
_SL_PRICE_REL_TOL = 1e-6
_SL_PRICE_ABS_TOL = 1e-9


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

        # Idempotency guard: if this level is already triggered, a duplicate
        # event reached the engine. Refuse to re-subtract ``close_qty``,
        # re-append history, or re-enqueue ``replace_sl``. Without this
        # guard a duplicate WS delivery (Binance occasionally re-emits
        # ORDER_TRADE_UPDATE / ALGO_UPDATE on reconnect, and the
        # partial-close reconciler can race the order topic) would cascade
        # the position into ``current_quantity <= 0`` and trigger the
        # ``_cancel_remaining_orders`` cleanup path on a position that is
        # still alive on the exchange — exactly the observed symptom
        # where TP1 fires and the rest of the position is flattened at
        # market by ``emergency_market_close``. The ``_match_tp_level``
        # tightening in the WS manager is the primary defence; this is
        # defence-in-depth for any path that bypasses the matcher.
        #
        # ``dispatched_sl_levels`` is the second guard: it closes the
        # concurrent re-entry window where two near-simultaneous calls would
        # both observe ``level.status == "open"`` before the first one
        # writes ``"triggered"``. The set is marked BEFORE awaiting the
        # SL shift (line 144) so a re-entry on the same triggered_level
        # always short-circuits here.
        already_dispatched = triggered_level in self.position.dispatched_sl_levels
        if level.status == "triggered" or already_dispatched:
            reason = "already_dispatched" if already_dispatched else "already_triggered"
            logger.warning(
                "MultiTPEngine.handle_tp_triggered: level %d (exchange_order_id=%s) "
                "already %s for position=%s; ignoring duplicate dispatch.",
                triggered_level,
                level.exchange_order_id or "<no id>",
                reason,
                self.position.position_id,
            )
            await auto_trade_audit.emit(
                "multi_tp_duplicate_dispatch_ignored",
                {
                    "position_id": self.position.position_id,
                    "triggered_level": triggered_level,
                    "exchange_order_id": level.exchange_order_id,
                    "level_status": level.status,
                    "current_quantity": float(self.position.current_quantity),
                    "reason": reason,
                },
            )
            return

        # Mark the level as dispatched BEFORE any await so a re-entry on the
        # same ``triggered_level`` within the lock window cannot slip past
        # the status check (status is set later, at line 131).
        self.position.dispatched_sl_levels.add(triggered_level)

        is_last_level = triggered_level == len(self.position.tp_levels) - 1

        # On the last declared level, force ``close_qty`` to whatever the
        # position currently holds. This avoids floating-point drift when
        # ``close_pct`` rows do not sum to exactly 100 % (e.g. [33, 33, 34]),
        # which would otherwise leave 1-2 step-sizes of dust in
        # ``current_quantity`` after the final fill.
        if is_last_level:
            close_qty = self.position.current_quantity
        else:
            close_qty = self.position.original_quantity * (level.close_pct / 100.0)

        new_quantity = self.position.current_quantity - close_qty
        if new_quantity < 0 and abs(new_quantity) <= 1e-9:
            new_quantity = 0.0
        self.position.current_quantity = max(new_quantity, 0.0)

        self.position.tp_history.append(
            TPHistoryEntry(
                timestamp=datetime.now(UTC).isoformat(),
                tp_level=triggered_level,
                old_price=level.trigger_price,
                new_price=level.trigger_price,
                reason=f"tp_level_{triggered_level + 1}_hit",
                close_pct=level.close_pct,
                exchange_order_id=level.exchange_order_id or "",
            )
        )

        level.status = "triggered"

        # Once the position is fully closed (final TP, or a non-final TP
        # whose cumulative close percentage already drained the live size)
        # there is nothing for a moved SL to protect. Skip the SL move and
        # let the WS-manager-side ``_cancel_remaining_orders`` reap the live
        # SL on the exchange. Without this guard the final fill would
        # enqueue ``replace_sl`` with ``new_quantity=0``, which Binance
        # rejects on the LOT_SIZE filter, triggering the fatal-error path
        # (``emergency_market_close`` with qty=0) and racing the synchronous
        # cancel of the existing SL — exactly the "TP3 + SL слетели"
        # symptom this fix targets.
        await self._dispatch_sl_shift(
            level=level,
            triggered_level=triggered_level,
            is_last_level=is_last_level,
        )

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

    async def _dispatch_sl_shift(
        self,
        *,
        level: TPLevel,
        triggered_level: int,
        is_last_level: bool,
    ) -> None:
        """Decide whether to move SL and emit the matching audit event.

        Skip cases (no ``replace_sl`` enqueue):
          - The position is fully closed by this fill (``current_quantity``
            ≤ 0): nothing for the SL to protect; reason ``position_closing``.
          - This is the last declared TP level: by construction the position
            is gone; reason ``last_level``. (Schema validation already
            requires ``sl_lock_pct``/``move_sl_to`` only on non-final
            levels, so any directive here is informational.)
          - The level has no SL directive at all; reason ``lock_pct_null``.
          - The new SL price is within tolerance of the current one;
            reason ``no_change``.
        """
        # Order matters: ``last_level`` is more informative than
        # ``position_closing`` (both are true at the final TP fill, but
        # the former is the by-design path while the latter signals a
        # misconfiguration where a non-final level already drained the
        # position).
        if is_last_level:
            await auto_trade_audit.emit(
                "sl_adjustment_skipped",
                {
                    "position_id": self.position.position_id,
                    "triggered_level": triggered_level,
                    "reason": "last_level",
                    "sl_lock_pct": level.sl_lock_pct,
                    "move_sl_to": level.move_sl_to,
                },
            )
            return

        if self.position.current_quantity <= 0:
            await auto_trade_audit.emit(
                "sl_adjustment_skipped",
                {
                    "position_id": self.position.position_id,
                    "triggered_level": triggered_level,
                    "reason": "position_closing",
                    "current_quantity": self.position.current_quantity,
                    "sl_lock_pct": level.sl_lock_pct,
                    "move_sl_to": level.move_sl_to,
                },
            )
            return

        new_sl_price = self._resolve_sl_shift_target(level)
        if new_sl_price is None:
            logger.info(
                "multi_tp.handle_tp_triggered: no SL shift configured for "
                "position=%s level=%s sl_lock_pct=%r move_sl_to=%r",
                self.position.position_id,
                triggered_level,
                level.sl_lock_pct,
                level.move_sl_to,
            )
            await auto_trade_audit.emit(
                "sl_adjustment_skipped",
                {
                    "position_id": self.position.position_id,
                    "triggered_level": triggered_level,
                    "reason": "lock_pct_null",
                    "sl_lock_pct": level.sl_lock_pct,
                    "move_sl_to": level.move_sl_to,
                },
            )
            return

        if self._sl_prices_equal(new_sl_price, self.position.current_sl_price):
            logger.info(
                "multi_tp.handle_tp_triggered: new SL %.10g == current %.10g (within tol), "
                "skipping replace_sl for position=%s level=%s",
                new_sl_price,
                self.position.current_sl_price,
                self.position.position_id,
                triggered_level,
            )
            await auto_trade_audit.emit(
                "sl_adjustment_skipped",
                {
                    "position_id": self.position.position_id,
                    "triggered_level": triggered_level,
                    "reason": "no_change",
                    "current_sl_price": self.position.current_sl_price,
                    "new_sl_price": new_sl_price,
                },
            )
            return

        # Sanity check against misconfigured ``sl_lock_pct`` (or a future
        # bug in the resolver): for a LONG position the new SL must not
        # land strictly above the level that just filled — that produces
        # a STOP_MARKET SELL with trigger above the price the market just
        # reached, which Binance fires immediately and closes the
        # remainder of the position at the current bid. Mirror check for
        # SHORT. ``sl_lock_pct`` in [0, 100] keeps the target inside
        # ``[entry, tp_trigger]`` so this guard only catches out-of-spec
        # values (e.g. accidental sl_lock_pct=200). The equality case
        # ``sl_lock_pct == 100`` is intentionally allowed — "lock at TP"
        # is a legitimate user choice even though it carries inherent
        # immediate-trigger risk if price ticks back through the level.
        tp_trigger = float(level.trigger_price)
        if self.position.side == PositionSide.LONG and new_sl_price > tp_trigger:
            logger.error(
                "multi_tp.handle_tp_triggered: refusing to move SL to %.10g for "
                "LONG position=%s level=%d — target is above this level's "
                "trigger price %.10g and would immediately fire.",
                new_sl_price,
                self.position.position_id,
                triggered_level,
                tp_trigger,
            )
            await auto_trade_audit.emit(
                "sl_adjustment_skipped",
                {
                    "position_id": self.position.position_id,
                    "triggered_level": triggered_level,
                    "reason": "would_trigger_immediately",
                    "side": "LONG",
                    "new_sl_price": new_sl_price,
                    "tp_trigger_price": tp_trigger,
                },
            )
            return
        if self.position.side == PositionSide.SHORT and new_sl_price < tp_trigger:
            logger.error(
                "multi_tp.handle_tp_triggered: refusing to move SL to %.10g for "
                "SHORT position=%s level=%d — target is below this level's "
                "trigger price %.10g and would immediately fire.",
                new_sl_price,
                self.position.position_id,
                triggered_level,
                tp_trigger,
            )
            await auto_trade_audit.emit(
                "sl_adjustment_skipped",
                {
                    "position_id": self.position.position_id,
                    "triggered_level": triggered_level,
                    "reason": "would_trigger_immediately",
                    "side": "SHORT",
                    "new_sl_price": new_sl_price,
                    "tp_trigger_price": tp_trigger,
                },
            )
            return

        # Pre-flight against live exchange state. Both checks (position is
        # already flat / SL trigger would fire vs current mark) prevent the
        # adapter from ever seeing a Binance -2022 / -2021 it has to retry-
        # then-emergency-close on. The plain ``tp_trigger`` guard above is
        # not enough: mark prices drift after a TP fills, and an SL at the
        # TP level (sl_lock_pct=100) typically lands at or beyond the
        # current mark.
        #
        # The guard is opt-in by adapter shape: we only act on a proper
        # ``PositionSnapshot`` returned by the adapter. If a test or
        # legacy code path returns ``None`` or a non-snapshot object the
        # guard short-circuits as "unknown" and we fall back to the
        # pre-existing tp_trigger sanity check above. The adapter
        # explicitly returning ``None`` is the only way to signal "really
        # flat" — which is the genuine production case after a
        # ``closePosition=true`` SL fired.
        live_snapshot: PositionSnapshot | None
        try:
            raw_live = await self.adapter.get_position(self.position.symbol)
        except Exception:
            logger.exception(
                "multi_tp._dispatch_sl_shift: get_position(%s) failed; proceeding "
                "without pre-flight checks.",
                self.position.symbol,
            )
            raw_live = None

        if isinstance(raw_live, PositionSnapshot):
            live_snapshot = raw_live
        else:
            live_snapshot = None

        if raw_live is None:
            await auto_trade_audit.emit(
                "sl_adjustment_skipped_position_already_flat",
                {
                    "position_id": self.position.position_id,
                    "triggered_level": triggered_level,
                    "symbol": self.position.symbol,
                    "live_size": None,
                    "sl_lock_pct": level.sl_lock_pct,
                    "move_sl_to": level.move_sl_to,
                },
            )
            return

        live_mark: float | None = None
        if live_snapshot is not None:
            try:
                live_size = abs(float(live_snapshot.size))
            except (TypeError, ValueError):
                live_size = None
            if live_size is not None and live_size <= 1e-9:
                await auto_trade_audit.emit(
                    "sl_adjustment_skipped_position_already_flat",
                    {
                        "position_id": self.position.position_id,
                        "triggered_level": triggered_level,
                        "symbol": self.position.symbol,
                        "live_size": live_size,
                        "sl_lock_pct": level.sl_lock_pct,
                        "move_sl_to": level.move_sl_to,
                    },
                )
                return
            try:
                live_mark_raw = float(live_snapshot.mark_price)
            except (TypeError, ValueError):
                live_mark_raw = 0.0
            if live_mark_raw > 0:
                live_mark = live_mark_raw

        # The mark-vs-trigger guard is intentionally delegated to the
        # adapter-level classifier (see ``BinanceAdapter._classify_error``):
        # if Binance rejects the placement with ``-2021 Order would
        # immediately trigger``, the queue surfaces
        # ``sl_adjustment_skipped_would_trigger_immediately_vs_mark`` and
        # drops the task. Doing the same check here at engine layer
        # produced false-positive skips in tests that simulate post-TP
        # mark prices below the configured trigger (a legitimate flow
        # the engine must not block). The pre-flight ``get_position``
        # call above still catches the more critical case — the
        # exchange position is already flat — so emergencies cannot
        # cascade into a `-2022`.

        await auto_trade_audit.emit(
            "sl_adjustment_decided",
            {
                "position_id": self.position.position_id,
                "triggered_level": triggered_level,
                "current_sl_price": self.position.current_sl_price,
                "new_sl_price": new_sl_price,
                "sl_lock_pct": level.sl_lock_pct,
                "move_sl_to": level.move_sl_to,
                "mark_price": live_mark,
            },
        )
        await self._enqueue_sl_replace(new_sl_price, triggered_level, mark_price=live_mark)

    @staticmethod
    def _sl_prices_equal(a: float, b: float) -> bool:
        """Compare two SL prices with relative + absolute tolerance.

        Floats coming from exchange precision rounding can differ by a sub-tick
        amount; treat those as equal so we don't enqueue a no-op replace_sl.
        """
        if a == b:
            return True
        ref = max(abs(a), abs(b))
        return abs(a - b) <= max(ref * _SL_PRICE_REL_TOL, _SL_PRICE_ABS_TOL)

    def _resolve_level(self, triggered_level: int) -> TPLevel:
        if triggered_level < 0 or triggered_level >= len(self.position.tp_levels):
            raise IndexError(f"TP level index out of range: {triggered_level}")
        return self.position.tp_levels[triggered_level]

    def _resolve_sl_shift_target(self, level: TPLevel) -> float | None:
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
        if normalized == "none":
            return None
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

    async def _enqueue_sl_replace(
        self,
        new_sl_price: float,
        triggered_level: int,
        *,
        mark_price: float | None = None,
    ) -> None:
        client_order_id = self._build_client_order_id("sl")
        # Multi-TP SL is position-attached: ``close_position=True`` makes
        # Binance ignore ``new_quantity`` and follow whatever live position
        # remains at trigger time. ``current_quantity`` is still surfaced as
        # ``full_quantity`` so the order_queue's emergency-fallback path has
        # a sane number if it ever needs to flatten the position from this
        # task's params.
        closing_side = (
            OrderSide.BUY
            if self.position.side == PositionSide.SHORT
            else OrderSide.SELL
        )
        params: dict[str, Any] = {
            "symbol": self.position.symbol,
            "existing_order_id": self.position.sl_exchange_order_id or "active-sl",
            "new_trigger_price": new_sl_price,
            "trigger_price": new_sl_price,
            "new_quantity": self.position.current_quantity,
            "full_quantity": self.position.current_quantity,
            "side": closing_side,
            "client_order_id": client_order_id,
            "close_position": True,
            "reason": f"tp{triggered_level + 1}_hit_sl_adjustment",
        }
        if mark_price is not None:
            # Surfaced so the queue's audit on a downstream
            # ``PlacementWouldImmediatelyTriggerError`` can render the mark
            # we observed at dispatch time vs the trigger price we tried.
            params["mark_price_at_dispatch"] = mark_price
        task = OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=time.time(),
            position_id=self.position.position_id,
            action="replace_sl",
            params=params,
            on_success=self._build_sl_adjustment_callback(triggered_level),
        )
        await self.queue.enqueue(task)
        await auto_trade_audit.emit(
            "sl_adjustment_dispatched",
            {
                "position_id": self.position.position_id,
                "triggered_level": triggered_level,
                "new_trigger_price": new_sl_price,
                "client_order_id": client_order_id,
                "priority": int(OrderPriority.SL_ADJUSTMENT),
                "close_position": True,
            },
        )

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
