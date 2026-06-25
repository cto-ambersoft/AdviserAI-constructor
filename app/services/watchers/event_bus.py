"""Redis pub/sub for watcher events."""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.exchange.adapter import OrderSide
from app.services.position.order_queue import OrderPriority, OrderTask
from app.services.watchers.indicator_watcher import WatcherEvent
from app.services.watchers.service import (
    compute_tightened_sl,
    extract_atr_value,
    get_order_queue,
    load_position_context,
    resolve_current_price,
    send_watcher_notification,
)
from app.worker.broker import broker

logger = logging.getLogger(__name__)

WATCHER_EVENT_CHANNEL = "position.indicator_trigger"


def _get_redis_client() -> Redis:
    return Redis(connection_pool=broker.connection_pool)


def _deserialize_watcher_event(raw_message: bytes | str) -> WatcherEvent:
    payload = json.loads(
        raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message,
    )
    return WatcherEvent(**payload)


def _serialize_watcher_event(event: WatcherEvent) -> dict[str, Any]:
    return {key: value for key, value in asdict(event).items() if value is not None}


async def publish_watcher_event(event: WatcherEvent) -> None:
    """Publish a watcher event to Redis pub/sub."""
    payload = json.dumps(_serialize_watcher_event(event), separators=(",", ":"), ensure_ascii=False)
    async with _get_redis_client() as redis:
        await redis.publish(WATCHER_EVENT_CHANNEL, payload)


async def subscribe_watcher_events(
    handler: Callable[[WatcherEvent], Awaitable[object]],
) -> None:
    """Subscribe to watcher events and route each event to the provided handler."""
    async with _get_redis_client() as redis:
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(WATCHER_EVENT_CHANNEL)
            async for message in pubsub.listen():
                if not message or message.get("type") != "message":
                    continue

                try:
                    event = _deserialize_watcher_event(message["data"])
                except Exception:
                    logger.exception("Failed to deserialize watcher event message.")
                    continue

                try:
                    await handler(event)
                except Exception:
                    logger.exception(
                        "Watcher event handler failed for position %s.",
                        event.position_id,
                    )
        finally:
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result


def _build_client_order_id(position_id: str, action: str) -> str:
    return f"{position_id}-{action}-{int(time.time() * 1000)}"


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _closing_order_side(raw_side: str) -> OrderSide:
    normalized = getattr(raw_side, "value", raw_side)
    if str(normalized).lower() == "short":
        return OrderSide.BUY
    return OrderSide.SELL


async def _claim_trigger(event: WatcherEvent) -> bool:
    """Per-(position, indicator, action) cooldown (review I4).

    Returns ``True`` if the trigger may proceed (claimed), ``False`` if a recent
    identical trigger is still within the cooldown window — so a persistent
    condition doesn't re-adjust SL / partial-close every tick. Fail-OPEN on Redis
    error (proceed): never silently drop a safety action because Redis blipped.
    """
    ttl = get_settings().watcher_trigger_cooldown_seconds
    if ttl <= 0:
        return True
    key = f"watcher:cooldown:{event.position_id}:{event.indicator}:{event.action}"
    try:
        async with _get_redis_client() as redis:
            claimed = await redis.set(key, "1", nx=True, ex=ttl)
            return bool(claimed)
    except Exception:
        logger.debug("watcher cooldown unavailable (Redis); allowing trigger")
        return True


async def handle_watcher_event(event: WatcherEvent) -> None:
    """Route watcher actions to the order queue or notification stub."""
    if event.action in ("tighten_sl", "close_partial") and not await _claim_trigger(event):
        logger.debug(
            "watcher trigger within cooldown; skipping %s for position %s",
            event.action,
            event.position_id,
        )
        return
    position = await load_position_context(event.position_id)

    if event.action == "tighten_sl":
        queue = await get_order_queue(position)
        atr_value = extract_atr_value(position, event)
        if atr_value is None:
            logger.warning(
                "Watcher tighten_sl skipped for position %s because ATR is unavailable.",
                position.position_id,
            )
            return

        snapshot = await queue.adapter.get_position(position.symbol)
        current_price = resolve_current_price(position, snapshot)
        if event.market_price is not None and event.market_price > 0:
            current_price = float(event.market_price)
        offset_multiplier = _coerce_float(
            event.action_params.get("sl_offset_atr", 1.5),
            default=1.5,
        )
        new_sl = compute_tightened_sl(
            position,
            current_price=current_price,
            atr_value=atr_value,
            offset_multiplier=offset_multiplier,
        )
        if new_sl is None:
            return

        if not position.sl_exchange_order_id:
            logger.warning(
                "Watcher tighten_sl skipped for position %s because active SL order id is missing.",
                position.position_id,
            )
            return

        await queue.enqueue(
            OrderTask(
                priority=OrderPriority.SL_ADJUSTMENT,
                created_at=time.time(),
                position_id=position.position_id,
                action="replace_sl",
                params={
                    "symbol": position.symbol,
                    "existing_order_id": position.sl_exchange_order_id,
                    "new_trigger_price": new_sl,
                    "new_quantity": position.current_quantity,
                    "client_order_id": _build_client_order_id(position.position_id, "watcher-sl"),
                    "reason": f"indicator:{event.indicator}:{event.condition}",
                },
            )
        )
        return

    if event.action == "close_partial":
        queue = await get_order_queue(position)
        close_pct = _coerce_float(event.action_params.get("close_pct", 25.0), default=25.0)
        if close_pct <= 0:
            logger.warning(
                "Watcher close_partial skipped for position %s because close_pct=%s is invalid.",
                position.position_id,
                close_pct,
            )
            return

        close_qty = position.current_quantity * min(close_pct, 100.0) / 100.0
        if close_qty <= 0:
            return

        await queue.enqueue(
            OrderTask(
                priority=OrderPriority.PARTIAL_CLOSE,
                created_at=time.time(),
                position_id=position.position_id,
                action="partial_close",
                params={
                    "symbol": position.symbol,
                    "side": _closing_order_side(position.side),
                    "quantity": close_qty,
                    "client_order_id": _build_client_order_id(
                        position.position_id,
                        "watcher-close",
                    ),
                    "reason": f"indicator:{event.indicator}:{event.condition}",
                },
            )
        )
        return

    if event.action == "alert":
        await send_watcher_notification(position.user_id, event)
        return

    logger.warning(
        "Unsupported watcher action '%s' for position %s.",
        event.action,
        position.position_id,
    )
