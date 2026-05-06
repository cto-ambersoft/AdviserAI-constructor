"""Priority order execution queue."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from app.services.exchange.adapter import ExchangeAdapter, OrderSide


class TransientExchangeError(Exception):
    """Retryable exchange error raised for transient failures."""


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
        self._pending_lock = asyncio.Lock()
        self._processing = False
        self._queue_poll_timeout = 0.1
        self._default_rate_limit_wait = 1.0

    @property
    def adapter(self) -> ExchangeAdapter:
        """Expose the bound adapter for higher-level orchestration."""
        return self._adapter

    async def enqueue(self, task: OrderTask) -> None:
        """Enqueue a task, deduplicating by position and action key."""
        key = self._task_key(task.position_id, task.action, task.params)

        async with self._pending_lock:
            existing = self._pending_tasks.get(key)
            if existing is not None:
                # Keep queue position and priority; only refresh payload with latest params.
                existing.params = dict(task.params)
                return

            task.params = dict(task.params)
            self._pending_tasks[key] = task
            await self._queue.put(task)

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

            try:
                has_headroom = await self._wait_for_rate_limit_headroom()
                if not has_headroom:
                    clear_pending = False
                    await self._queue.put(task)
                    continue

                result = await self._execute_task(task)
                if task.on_success is not None:
                    callback_result = task.on_success(result)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            except TransientExchangeError as error:
                if task.retry_count < task.max_retries:
                    task.retry_count += 1
                    clear_pending = False
                    await asyncio.sleep(self._compute_backoff(task.retry_count))
                    await self._queue.put(task)
                else:
                    await self._handle_max_retries(task, error)
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
            )

        if task.action == "replace_sl":
            return await self._adapter.cancel_and_replace_sl(
                symbol=self._require_str(params, "symbol"),
                existing_order_id=self._require_str(params, "existing_order_id"),
                new_trigger_price=self._require_float(params, "new_trigger_price"),
                new_quantity=self._require_float(params, "new_quantity"),
                client_order_id=self._require_str(params, "client_order_id"),
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
            return await self._adapter.partial_close(
                symbol=self._require_str(params, "symbol"),
                side=self._require_side(params, "side"),
                quantity=self._resolve_emergency_quantity(params),
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
        """Hook for fatal task errors; keeps queue running for subsequent tasks."""
        _ = (task, error)

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
    def _resolve_emergency_quantity(params: dict[str, Any]) -> float:
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

        raise ValueError("Missing emergency close quantity parameter")
