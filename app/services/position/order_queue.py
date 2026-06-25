"""Priority order execution queue."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from app.services.exchange.adapter import (
    ExchangeAdapter,
    OrderSide,
    PlacementWouldImmediatelyTriggerError,
    PositionAlreadyFlatError,
    PositionSnapshot,
)
from app.services.exchange.adapter import (
    TransientExchangeError as _AdapterTransientExchangeError,
)

logger = logging.getLogger(__name__)


# Below this absolute size the exchange position is treated as "flat" for
# the purpose of emergency close decisions. Exchanges quantize position size
# to step-size; sub-step residuals routinely persist after a fully-closed
# position and would otherwise drive reduce-only orders that get rejected
# with -2022 / equivalent. Picked an order of magnitude below typical
# step-sizes (BTC 0.001, ETH 0.001, alts 0.1) so it is a safety floor, not
# a tradable threshold. Symbols with sub-millicontract step sizes should
# override via the adapter (followup; see plan group 6.3).
EMERGENCY_CLOSE_DUST_EPSILON: float = 1e-6


# Hook for surfacing fatal task failures via auto_trade_event.
# Wired by AutoTradeService at startup; left as None for unit tests that don't
# care about audit emission. Signature: (task, error) -> awaitable[None].
FatalErrorAuditHook = Callable[["OrderTask", Exception], Awaitable[None]]
_audit_hook: FatalErrorAuditHook | None = None

# Hook for emitting structured safety events from the queue (e.g.
# "emergency close skipped, position already flat"). Distinct from the fatal-
# error hook because these are not failures — they are intentional skips
# whose payload is event-shaped (dict) rather than (task, error).
# Signature: (event_type, payload) -> awaitable[None].
SafetyAuditHook = Callable[[str, dict[str, Any]], Awaitable[None]]
_safety_audit_hook: SafetyAuditHook | None = None


def set_fatal_error_audit_hook(hook: FatalErrorAuditHook | None) -> None:
    """Register a global hook called for every non-transient task failure."""
    global _audit_hook
    _audit_hook = hook


def set_safety_audit_hook(hook: SafetyAuditHook | None) -> None:
    """Register a global hook called for non-fatal safety events from the queue."""
    global _safety_audit_hook
    _safety_audit_hook = hook


async def _emit_safety_audit(event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort safety audit emit. Never raises."""
    if _safety_audit_hook is None:
        logger.debug("order_queue safety audit (no hook): %s %s", event_type, payload)
        return
    try:
        await _safety_audit_hook(event_type, payload)
    except Exception:
        logger.exception("order_queue safety audit hook for %s failed", event_type)


# Re-export the canonical ``TransientExchangeError`` from the adapter
# boundary so external imports continue to work. Concrete adapters raise the
# adapter-side class directly without needing to import from
# ``app.services.position``.
TransientExchangeError = _AdapterTransientExchangeError


class OrderPriority(IntEnum):
    """Lower number means higher execution priority."""

    EMERGENCY_SL = 0
    EMERGENCY_CLOSE = 1
    SL_ADJUSTMENT = 10
    TP_ADJUSTMENT = 20
    PARTIAL_CLOSE = 30
    NEW_CONDITIONAL = 40
    CANCEL_ORDER = 50


@dataclass(order=True)
class OrderTask:
    """Queue item sorted by priority first and creation timestamp second."""

    priority: OrderPriority = field(compare=True)
    created_at: float = field(compare=True)
    position_id: str = field(compare=False)
    action: str = field(compare=False)
    params: dict[str, Any] = field(compare=False, default_factory=dict)
    retry_count: int = field(compare=False, default=0)
    max_retries: int = field(compare=False, default=3)
    on_success: Callable[[Any], Awaitable[Any] | Any] | None = field(
        compare=False,
        default=None,
        repr=False,
    )


class OrderExecutionQueue:
    """Per-account priority queue for exchange order execution."""

    def __init__(self, adapter: ExchangeAdapter, account_id: str) -> None:
        self._adapter = adapter
        self._account_id = account_id
        self._queue: asyncio.PriorityQueue[OrderTask] = asyncio.PriorityQueue()
        self._pending_tasks: dict[str, OrderTask] = {}
        # Tombstoned task keys are dropped silently by the dequeue loop. Used
        # by ``purge_pending`` so that a position transitioning to CLOSED can
        # cancel in-flight protective-order tasks before ``_cancel_remaining_orders``
        # races them on the exchange side.
        self._tombstoned_keys: set[str] = set()
        # Per-key events flagged "task currently executing" so
        # ``await_quiescent`` can wait for in-flight protective tasks before
        # ``WebSocketManager._cancel_remaining_orders`` cancels their
        # exchange-side orders. Without this the on-exchange cleanup races
        # ``cancel_and_replace_sl`` and deletes the new SL the task just
        # placed.
        self._executing_keys: dict[str, asyncio.Event] = {}
        self._pending_lock = asyncio.Lock()
        self._processing = False
        self._queue_poll_timeout = 0.1
        self._default_rate_limit_wait = 1.0
        # Multi-TP ``replace_sl`` tasks that arrive within this window for
        # the same position get coalesced into the latest-intent one even
        # when target prices differ. Production logs showed four such
        # dispatches in the same millisecond for a single TP1 fill.
        self._quickfire_coalesce_window_seconds: float = 0.5

    @property
    def adapter(self) -> ExchangeAdapter:
        """Expose the bound adapter for higher-level orchestration."""
        return self._adapter

    async def enqueue(self, task: OrderTask) -> None:
        """Enqueue a task, deduplicating by position and action key.

        For SL-related actions (place_sl, replace_sl) we coalesce into the
        existing pending task by refreshing its params, but we also bump
        ``created_at`` and keep the higher priority so the latest-intent
        replacement is processed first. Different SL target prices produce
        distinct keys (see ``_task_key``) and are therefore NOT coalesced.
        """
        key = self._task_key(task.position_id, task.action, task.params)

        async with self._pending_lock:
            # Quick-fire dedup defence-in-depth: when ``replace_sl`` is
            # dispatched multiple times for the same position within a tight
            # window, coalesce into the latest-intent task even when target
            # prices differ. In the prod incident four SL adjustments fired
            # in the same millisecond for one TP1 fill — without this guard
            # they each produced their own task because ``_task_key``
            # discriminates by target price. We only coalesce across multi-
            # TP ``reason`` prefixes; trailing/breakeven/volatility sources
            # carry distinct reasons and must not be folded into a multi-TP
            # move (or vice versa).
            if task.action == "replace_sl" and self._is_multi_tp_reason(
                task.params.get("reason")
            ):
                coalesce_target = self._find_quickfire_replace_sl(
                    position_id=task.position_id,
                    created_at=task.created_at,
                    window_seconds=self._quickfire_coalesce_window_seconds,
                )
                if coalesce_target is not None:
                    coalesce_key, existing_task = coalesce_target
                    previous_target = existing_task.params.get(
                        "new_trigger_price"
                    ) or existing_task.params.get("trigger_price")
                    new_target = task.params.get("new_trigger_price") or task.params.get(
                        "trigger_price"
                    )
                    existing_task.params = dict(task.params)
                    existing_task.created_at = task.created_at
                    if task.priority < existing_task.priority:
                        existing_task.priority = task.priority
                    # Rekey when the target price (which is part of the key)
                    # changed so subsequent calls coalesce against the new
                    # key as well.
                    new_key = self._task_key(task.position_id, task.action, task.params)
                    if new_key != coalesce_key:
                        self._pending_tasks.pop(coalesce_key, None)
                        self._pending_tasks[new_key] = existing_task
                    logger.info(
                        "[%s] order_queue.enqueue: quickfire-coalesced replace_sl "
                        "for position=%s (previous target=%s, new target=%s).",
                        self._account_id,
                        task.position_id,
                        previous_target,
                        new_target,
                    )
                    await _emit_safety_audit(
                        "replace_sl_coalesced_inflight",
                        {
                            "position_id": task.position_id,
                            "previous_target": previous_target,
                            "new_target": new_target,
                            "window_seconds": self._quickfire_coalesce_window_seconds,
                        },
                    )
                    return

            existing = self._pending_tasks.get(key)
            if existing is not None:
                existing.params = dict(task.params)
                if task.action in {"place_sl", "replace_sl"}:
                    # Latest intent wins for safety-critical SL ops.
                    existing.created_at = task.created_at
                    if task.priority < existing.priority:
                        existing.priority = task.priority
                    logger.debug(
                        "order_queue.enqueue: coalesced %s for position=%s "
                        "(latest target=%s)",
                        task.action,
                        task.position_id,
                        task.params.get("new_trigger_price")
                        or task.params.get("trigger_price"),
                    )
                return

            task.params = dict(task.params)
            self._pending_tasks[key] = task
            await self._queue.put(task)

    @staticmethod
    def _is_multi_tp_reason(reason: Any) -> bool:
        """Return True when the task's ``reason`` looks like a multi-TP SL move.

        Multi-TP SL repositioning sets ``reason="tp{N}_hit_sl_adjustment"``
        (see ``MultiTPEngine._enqueue_sl_replace``). Trailing / breakeven /
        volatility flows use ``reason="realtime_pipeline:<source>"``. We
        coalesce across multi-TP tasks (defence against the cascade) but
        keep trailing-vs-multi-TP separate so a trailing move doesn't
        silently override a multi-TP lock-in or vice versa.
        """
        if not isinstance(reason, str):
            return False
        return reason.startswith("tp") and "sl_adjustment" in reason

    def _find_quickfire_replace_sl(
        self,
        *,
        position_id: str,
        created_at: float,
        window_seconds: float,
    ) -> tuple[str, OrderTask] | None:
        """Return the most recent pending multi-TP ``replace_sl`` for the same
        position whose ``created_at`` is within ``window_seconds`` of the
        new task. Caller already holds ``_pending_lock``.
        """
        for existing_key, existing in self._pending_tasks.items():
            if existing.position_id != position_id:
                continue
            if existing.action != "replace_sl":
                continue
            if not self._is_multi_tp_reason(existing.params.get("reason")):
                continue
            if abs(existing.created_at - created_at) > window_seconds:
                continue
            return existing_key, existing
        return None

    async def start_processing(self) -> None:
        """Run processing loop until stop is called."""
        if self._processing:
            return

        self._processing = True
        while self._processing:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=self._queue_poll_timeout)
            except TimeoutError:
                continue

            key = self._task_key(task.position_id, task.action, task.params)
            clear_pending = True

            # Skip tombstoned tasks — purge_pending dropped them while they
            # were already on the priority queue. The pending-tasks map has
            # already been pruned, so just discard.
            async with self._pending_lock:
                if key in self._tombstoned_keys:
                    self._tombstoned_keys.discard(key)
                    self._queue.task_done()
                    continue

            try:
                has_headroom = await self._wait_for_rate_limit_headroom()
                if not has_headroom:
                    clear_pending = False
                    await self._queue.put(task)
                    continue

                # Mark this key as executing so ``await_quiescent`` can block
                # on it. Cleared in the ``finally`` block below — both happy-
                # path and exception unwind go through the cleanup.
                exec_event = asyncio.Event()
                async with self._pending_lock:
                    self._executing_keys[key] = exec_event

                try:
                    result = await self._execute_task(task)
                    if task.on_success is not None:
                        callback_result = task.on_success(result)
                        if inspect.isawaitable(callback_result):
                            await callback_result
                finally:
                    exec_event.set()
                    async with self._pending_lock:
                        if self._executing_keys.get(key) is exec_event:
                            self._executing_keys.pop(key, None)
            except TransientExchangeError as error:
                if task.retry_count < task.max_retries:
                    task.retry_count += 1
                    clear_pending = False
                    await asyncio.sleep(self._compute_backoff(task.retry_count))
                    await self._queue.put(task)
                else:
                    await self._handle_max_retries(task, error)
            except PlacementWouldImmediatelyTriggerError as error:
                # The trigger price violates the exchange's immediate-trigger
                # rule (Binance -2021 / -4131). Retrying with the same target
                # cannot succeed; escalating to emergency_market_close would
                # flatten the position over what is fundamentally a config
                # warning. Surface it and drop the task.
                logger.warning(
                    "[%s] %s for position=%s skipped: would immediately trigger "
                    "(code=%s, requested=%s, mark=%s).",
                    self._account_id,
                    task.action,
                    task.position_id,
                    error.code,
                    error.requested_trigger,
                    error.mark_price,
                )
                await _emit_safety_audit(
                    "sl_adjustment_skipped_would_trigger_immediately_vs_mark",
                    {
                        "position_id": task.position_id,
                        "action": task.action,
                        "code": error.code,
                        "requested_trigger": error.requested_trigger,
                        "mark_price": error.mark_price,
                        "payload": error.payload,
                    },
                )
            except PositionAlreadyFlatError as error:
                # The exchange position is gone — nothing to protect or close.
                # Drop the task without enqueuing an emergency close (which
                # would itself fail with -2022 against the flat position).
                logger.warning(
                    "[%s] %s for position=%s skipped: position already flat on exchange.",
                    self._account_id,
                    task.action,
                    task.position_id,
                )
                await _emit_safety_audit(
                    "sl_adjustment_skipped_position_already_flat",
                    {
                        "position_id": task.position_id,
                        "action": task.action,
                        "symbol": task.params.get("symbol"),
                        "code": error.code,
                        "payload": error.payload,
                    },
                )
            except Exception as error:
                # Non-transient task failures are treated as fatal for this task.
                await self._handle_fatal_error(task, error)
            finally:
                if clear_pending:
                    async with self._pending_lock:
                        self._pending_tasks.pop(key, None)
                self._queue.task_done()

        self._processing = False

    async def stop(self) -> None:
        """Stop processing loop gracefully."""
        self._processing = False

    async def await_quiescent(
        self,
        position_id: str,
        actions: set[str],
        timeout: float = 2.0,
    ) -> bool:
        """Block until every currently-executing task for the position whose
        action is in ``actions`` finishes (or ``timeout`` elapses).

        Returns True if all matching in-flight tasks completed within the
        deadline, False on timeout. Callers can use the timeout result to
        emit an audit (e.g. ``cancel_remaining_orders_quiesce_timeout``).

        Without this hook, ``WebSocketManager._cancel_remaining_orders``
        races a mid-flight ``cancel_and_replace_sl`` (which is
        place-first / cancel-last on Binance) — the cleanup DELETEs the new
        SL the queue task just placed, leaving the position unprotected.
        """
        async with self._pending_lock:
            relevant_events: list[asyncio.Event] = []
            for key, event in self._executing_keys.items():
                if not key.startswith(f"{position_id}:"):
                    continue
                # ``key`` format is ``{position_id}:{action}[:...]`` — split
                # once on ``:`` to get the action segment.
                segments = key.split(":", 2)
                if len(segments) < 2:
                    continue
                if segments[1] not in actions:
                    continue
                relevant_events.append(event)

        if not relevant_events:
            return True

        pending_waits = [asyncio.create_task(event.wait()) for event in relevant_events]
        try:
            _, pending = await asyncio.wait(pending_waits, timeout=timeout)
        finally:
            for task_wait in pending_waits:
                if not task_wait.done():
                    task_wait.cancel()
        if pending:
            logger.warning(
                "[%s] await_quiescent: %d task(s) for position=%s actions=%s still "
                "running after %.2fs timeout.",
                self._account_id,
                len(pending),
                position_id,
                sorted(actions),
                timeout,
            )
            return False
        return True

    async def purge_pending(
        self,
        position_id: str,
        actions: set[str],
    ) -> list[str]:
        """Drop pending tasks for the given position whose action matches.

        Used when a position transitions to CLOSED: the cleanup path in
        ``WebSocketManager._cancel_remaining_orders`` will cancel SL/TP orders
        on the exchange directly, and we must not let stale ``replace_sl`` /
        ``place_sl`` / ``place_tp`` tasks race with that cancellation. Removes
        the matching keys from ``_pending_tasks`` and tombstones them so the
        dequeue loop skips them when it eventually pops the priority queue.

        Returns the list of task keys that were purged (useful for tests and
        observability).
        """
        purged: list[str] = []
        async with self._pending_lock:
            for key in list(self._pending_tasks.keys()):
                if not key.startswith(f"{position_id}:"):
                    continue
                task = self._pending_tasks[key]
                if task.action not in actions:
                    continue
                self._pending_tasks.pop(key, None)
                self._tombstoned_keys.add(key)
                purged.append(key)
        if purged:
            logger.info(
                "[%s] order_queue.purge_pending: dropped %d pending tasks for "
                "position=%s actions=%s",
                self._account_id,
                len(purged),
                position_id,
                sorted(actions),
            )
        return purged

    async def _execute_task(self, task: OrderTask) -> Any:
        """Route task action to matching exchange adapter call."""
        params = task.params

        if task.action == "place_sl":
            return await self._adapter.place_stop_loss(
                symbol=self._require_str(params, "symbol"),
                side=self._require_side(params, "side"),
                quantity=self._require_float(params, "quantity"),
                trigger_price=self._require_float(params, "trigger_price"),
                client_order_id=self._require_str(params, "client_order_id"),
                reduce_only=self._require_bool(params, "reduce_only", default=True),
                close_position=self._require_bool(params, "close_position", default=False),
            )

        if task.action == "replace_sl":
            return await self._adapter.cancel_and_replace_sl(
                symbol=self._require_str(params, "symbol"),
                existing_order_id=self._require_str(params, "existing_order_id"),
                new_trigger_price=self._require_float(params, "new_trigger_price"),
                new_quantity=self._require_float(params, "new_quantity"),
                client_order_id=self._require_str(params, "client_order_id"),
                close_position=self._require_bool(params, "close_position", default=True),
            )

        if task.action == "place_tp":
            return await self._adapter.place_take_profit(
                symbol=self._require_str(params, "symbol"),
                side=self._require_side(params, "side"),
                quantity=self._require_float(params, "quantity"),
                trigger_price=self._require_float(params, "trigger_price"),
                client_order_id=self._require_str(params, "client_order_id"),
                reduce_only=self._require_bool(params, "reduce_only", default=True),
                limit_price=self._optional_float(params, "limit_price"),
            )

        if task.action == "replace_tp":
            return await self._adapter.cancel_and_replace_tp(
                symbol=self._require_str(params, "symbol"),
                existing_order_id=self._require_str(params, "existing_order_id"),
                new_trigger_price=self._require_float(params, "new_trigger_price"),
                new_quantity=self._require_float(params, "new_quantity"),
                client_order_id=self._require_str(params, "client_order_id"),
                limit_price=self._optional_float(params, "limit_price"),
            )

        if task.action == "partial_close":
            return await self._adapter.partial_close(
                symbol=self._require_str(params, "symbol"),
                side=self._require_side(params, "side"),
                quantity=self._require_float(params, "quantity"),
                client_order_id=self._require_str(params, "client_order_id"),
                order_type=self._require_str(params, "order_type", default="market"),
                price=self._optional_float(params, "price"),
            )

        if task.action == "emergency_market_close":
            symbol = self._require_str(params, "symbol")
            # Re-query the exchange before issuing a reduce-only close.
            # The task params carry whatever ``full_quantity`` was captured
            # when the failing ``replace_sl`` was enqueued; in production
            # that quantity is routinely stale by the time the emergency
            # close runs (TP fills mid-flight, position already auto-closed
            # by a ``closePosition=true`` SL). Sending a reduce-only order
            # for a stale quantity against a flat position is exactly the
            # Binance error -2022 path observed in the incident.
            #
            # The re-query is opt-in by adapter shape: a real
            # ``PositionSnapshot`` is the only thing that drives the
            # "skip if flat" branch. Mocks that don't configure
            # ``get_position`` return a Mock instance — we treat that as
            # "unknown" and fall back to the legacy params-only behaviour
            # (close ``full_quantity`` if positive, else skip). This keeps
            # the new safety net opt-in without breaking the existing test
            # surface.
            try:
                raw_live = await self._adapter.get_position(symbol)
            except Exception:
                logger.exception(
                    "[%s] emergency_market_close: get_position failed for %s; "
                    "falling back to task params.",
                    self._account_id,
                    symbol,
                )
                raw_live = None

            live: PositionSnapshot | None
            if isinstance(raw_live, PositionSnapshot):
                live = raw_live
            else:
                live = None

            if raw_live is None:
                # Adapter explicitly reports no position. Skip.
                logger.warning(
                    "[%s] emergency_market_close for position=%s symbol=%s skipped: "
                    "exchange reports no live position.",
                    self._account_id,
                    task.position_id,
                    symbol,
                )
                await _emit_safety_audit(
                    "emergency_close_skipped_position_flat",
                    {
                        "position_id": task.position_id,
                        "symbol": symbol,
                        "live_size": None,
                        "requested_quantity": params.get("full_quantity")
                        or params.get("quantity")
                        or params.get("new_quantity"),
                        "reason": params.get("reason"),
                    },
                )
                return None

            live_size: float | None = None
            if live is not None:
                try:
                    live_size = abs(float(live.size))
                except (TypeError, ValueError):
                    live_size = None
                if live_size is not None and live_size <= EMERGENCY_CLOSE_DUST_EPSILON:
                    logger.warning(
                        "[%s] emergency_market_close for position=%s symbol=%s skipped: "
                        "exchange reports flat position (live=%s).",
                        self._account_id,
                        task.position_id,
                        symbol,
                        live_size,
                    )
                    await _emit_safety_audit(
                        "emergency_close_skipped_position_flat",
                        {
                            "position_id": task.position_id,
                            "symbol": symbol,
                            "live_size": live_size,
                            "requested_quantity": params.get("full_quantity")
                            or params.get("quantity")
                            or params.get("new_quantity"),
                            "reason": params.get("reason"),
                        },
                    )
                    return None

            requested = self._resolve_emergency_quantity(params)
            if requested <= 0 and live_size is None:
                # Legacy: no usable params quantity and no live snapshot —
                # mirror the pre-fix behaviour and skip.
                logger.warning(
                    "[%s] emergency_market_close for position=%s skipped: "
                    "qty resolved to 0 (position presumed already flat).",
                    self._account_id,
                    task.position_id,
                )
                return None
            if requested <= 0:
                # No usable quantity in params but the exchange position is
                # still alive — close whatever is live to avoid leaving the
                # position unprotected.
                quantity = live_size or 0.0
            elif live_size is not None:
                quantity = min(requested, live_size)
            else:
                # Unknown live size (mock or unsupported adapter) — fall
                # back to the requested quantity from params.
                quantity = requested
            return await self._adapter.partial_close(
                symbol=symbol,
                side=self._require_side(params, "side"),
                quantity=quantity,
                client_order_id=self._require_str(
                    params,
                    "client_order_id",
                    default=self._build_emergency_client_order_id(task.position_id),
                ),
                order_type="market",
                price=None,
            )

        if task.action == "cancel_order":
            return await self._adapter.cancel_conditional_order(
                symbol=self._require_str(params, "symbol"),
                order_id=self._require_str(params, "order_id"),
            )

        raise ValueError(f"Unsupported order task action: {task.action}")

    async def _wait_for_rate_limit_headroom(self) -> bool:
        """Wait until adapter confirms order placement headroom."""
        while self._processing:
            if await self._adapter.can_place_order():
                return True

            wait_time = self._default_rate_limit_wait
            rate_state = await self._adapter.get_rate_limit_state()
            retry_after = rate_state.retry_after
            if retry_after is not None and retry_after > 0:
                wait_time = retry_after

            await asyncio.sleep(wait_time)

        return False

    async def _handle_max_retries(
        self,
        task: OrderTask,
        error: TransientExchangeError,
    ) -> None:
        """Escalate SL failures to emergency market close."""
        if task.action not in {"place_sl", "replace_sl"}:
            return

        if not self._has_positive_emergency_quantity(task.params):
            logger.warning(
                "[%s] Skipping emergency close for position=%s: no positive "
                "quantity in failed task params (likely already flat).",
                self._account_id,
                task.position_id,
            )
            return

        emergency_task = OrderTask(
            priority=OrderPriority.EMERGENCY_CLOSE,
            created_at=time.time(),
            position_id=task.position_id,
            action="emergency_market_close",
            params={
                "symbol": task.params.get("symbol"),
                "side": task.params.get("side"),
                "full_quantity": task.params.get(
                    "full_quantity",
                    task.params.get("quantity", task.params.get("new_quantity")),
                ),
                "client_order_id": task.params.get("client_order_id"),
                "reason": f"SL placement failed after retries: {error}",
            },
        )
        await self.enqueue(emergency_task)

    async def _handle_fatal_error(self, task: OrderTask, error: Exception) -> None:
        """Surface fatal task errors and escalate SL failures to emergency close.

        - Logs at ERROR with task params for every fatal failure.
        - Calls the registered audit hook (if any) so an auto_trade_event can be
          written by callers that own a DB session.
        - For ``place_sl``/``replace_sl`` failures, enqueues
          ``emergency_market_close`` so an unprotected position is flattened
          rather than left exposed silently.
        """
        logger.error(
            "[%s] Order task failed (action=%s position=%s retries=%d): %s",
            self._account_id,
            task.action,
            task.position_id,
            task.retry_count,
            error,
            extra={"order_task_params": dict(task.params)},
        )

        if _audit_hook is not None:
            try:
                await _audit_hook(task, error)
            except Exception:
                logger.exception(
                    "[%s] Audit hook for order_queue fatal error itself failed.",
                    self._account_id,
                )

        if task.action in {"place_sl", "replace_sl"}:
            if not self._has_positive_emergency_quantity(task.params):
                logger.warning(
                    "[%s] Skipping emergency close for position=%s: no positive "
                    "quantity in failed task params (likely already flat).",
                    self._account_id,
                    task.position_id,
                )
                return
            emergency_task = OrderTask(
                priority=OrderPriority.EMERGENCY_CLOSE,
                created_at=time.time(),
                position_id=task.position_id,
                action="emergency_market_close",
                params={
                    "symbol": task.params.get("symbol"),
                    "side": task.params.get("side"),
                    "full_quantity": task.params.get(
                        "full_quantity",
                        task.params.get("quantity", task.params.get("new_quantity")),
                    ),
                    "client_order_id": task.params.get("client_order_id"),
                    "reason": f"SL placement failed (non-transient): {error}",
                },
            )
            await self.enqueue(emergency_task)

    @staticmethod
    def _task_key(position_id: str, action: str, params: dict[str, Any] | None = None) -> str:
        payload = params or {}
        base_key = f"{position_id}:{action}"

        if action == "place_tp":
            level = payload.get("level")
            if level is not None:
                return f"{base_key}:level:{level}"

        if action == "replace_tp":
            existing_order_id = payload.get("existing_order_id")
            if existing_order_id:
                return f"{base_key}:order:{existing_order_id}"

        if action in {"place_sl", "replace_sl"}:
            # Different target prices must NOT coalesce — that would silently lose
            # a safety-critical SL move. Same target prices coalesce so retries
            # / duplicate emits don't pile up requests.
            target = payload.get("new_trigger_price", payload.get("trigger_price"))
            if target is not None:
                try:
                    rounded = f"{float(target):.10g}"
                except (TypeError, ValueError):
                    rounded = str(target)
                return f"{base_key}:target:{rounded}"

        if action == "cancel_order":
            order_id = payload.get("order_id")
            if order_id:
                return f"{base_key}:order:{order_id}"

        return base_key

    @staticmethod
    def _compute_backoff(retry_count: int) -> float:
        return float(min(0.5 * (2**retry_count), 10.0))

    def _build_emergency_client_order_id(self, position_id: str) -> str:
        timestamp_ms = int(time.time() * 1000)
        return f"{self._account_id}-{position_id}-emergency-{timestamp_ms}"

    @staticmethod
    def _require_str(params: dict[str, Any], key: str, default: str | None = None) -> str:
        value = params.get(key, default)
        if isinstance(value, str) and value:
            return value
        raise ValueError(f"Missing or invalid string parameter: {key}")

    @staticmethod
    def _require_float(params: dict[str, Any], key: str) -> float:
        value = params.get(key)
        if value is None:
            raise ValueError(f"Missing numeric parameter: {key}")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric parameter: {key}") from exc

    @staticmethod
    def _optional_float(params: dict[str, Any], key: str) -> float | None:
        value = params.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric parameter: {key}") from exc

    @staticmethod
    def _require_side(params: dict[str, Any], key: str) -> OrderSide:
        value = params.get(key)
        if isinstance(value, OrderSide):
            return value
        if isinstance(value, str):
            raw = value.strip().lower()
            try:
                return OrderSide(raw)
            except ValueError as exc:
                raise ValueError(f"Invalid order side for {key}: {value}") from exc
        raise ValueError(f"Missing order side parameter: {key}")

    @staticmethod
    def _require_bool(params: dict[str, Any], key: str, default: bool) -> bool:
        value = params.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
        return bool(value)

    @staticmethod
    def _has_positive_emergency_quantity(params: dict[str, Any]) -> bool:
        for key in ("full_quantity", "quantity", "new_quantity"):
            value = params.get(key)
            if value is None:
                continue
            try:
                if float(value) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _resolve_emergency_quantity(params: dict[str, Any]) -> float:
        """Pick a positive close quantity from any of the conventional keys.

        Returns ``0.0`` when no positive value is present. Callers must
        treat that as "the position is already flat" and skip the close
        rather than raising — emergency tasks built from a fully-closed
        position carry zero qty by design (e.g. when a final-TP fill
        already drained the live size). This is defensive: the canonical
        path now also avoids enqueuing emergency closes for qty=0 (see
        ``_handle_fatal_error`` and ``_handle_max_retries`` callers).
        """
        for key in ("full_quantity", "quantity", "new_quantity"):
            value = params.get(key)
            if value is None:
                continue
            try:
                quantity = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid numeric parameter: {key}") from exc
            if quantity > 0:
                return quantity

        return 0.0
