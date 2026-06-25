"""Per-user SSE event channel over Redis pub/sub (B3).

Risk/governance events emitted on the trade path (``AutoTradeService._emit_event``)
are *also* published to a per-user Redis channel; the ``/events/stream`` endpoint
subscribes to it and pushes them to the Live Monitor. Publishing is strictly
best-effort: the durable record is the ``auto_trade_events`` row, so a Redis hiccup
must never affect trading. Mirrors the watcher event-bus Redis pattern.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.worker.broker import broker

logger = logging.getLogger(__name__)

# Key under ``session.info`` holding events queued by ``queue_user_event`` and
# drained by the after-commit listener below.
_PENDING_KEY = "_sse_pending"

# Risk/governance events surfaced to the user's live stream. Mirrors the notifiable
# RISK_EVENTS plus the pre-trade/degraded signals the Live Monitor cares about.
STREAMABLE_EVENTS: frozenset[str] = frozenset(
    {
        "risk_blocked",
        "risk_check_degraded",
        "kpi_guard_triggered",
        "strategy_auto_paused",
        "kill_switch_triggered",
        "position_emergency_closed_unprotected",
        "data_stale",
        "data_stale_blocked",
        "portfolio_dd_halt",
        # T15 (W12g): periodic Live-Monitor KPI snapshot pushed over SSE so the
        # dashboard updates from the stream instead of 30s polling.
        "portfolio_kpi",
        # B5 (W10) Promotion Pipeline + B6 (W12) anomaly detection.
        "promotion_ready",
        "strategy_promoted",
        "strategy_demoted",
        "promotion_gate_failed",
        "strategy_anomaly_detected",
    }
)


def user_channel(user_id: int) -> str:
    return f"events:user:{user_id}"


def _get_redis_client() -> Redis:
    return Redis(connection_pool=broker.connection_pool)


async def publish_user_event(
    *,
    user_id: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    message: str | None = None,
) -> None:
    """Best-effort publish of a streamable event to the user's channel.

    Filters to ``STREAMABLE_EVENTS`` first (a cheap set lookup), so the common
    non-streamable emits never touch Redis. NEVER raises — a publish failure is
    logged at debug and swallowed so it can't affect the trade path that emitted it.
    """
    if event_type not in STREAMABLE_EVENTS:
        return
    try:
        # ``default=str`` + inside the try: a non-JSON-native payload value (Decimal,
        # datetime, …) must never raise into the trade path that emitted the event.
        body = json.dumps(
            {"event_type": event_type, "payload": payload or {}, "message": message},
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        async with _get_redis_client() as redis:
            await redis.publish(user_channel(user_id), body)
    except Exception:
        logger.debug("SSE publish failed for user_id=%s event=%s", user_id, event_type)


def queue_user_event(
    session: Any,
    *,
    user_id: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    message: str | None = None,
) -> None:
    """Queue a streamable event to be published AFTER the session commits (I1).

    Publishing on commit (not at emit time) means the live stream mirrors only rows
    that are actually durable — a ``commit=False`` emit that later rolls back never
    produces a phantom SSE event. Filters to ``STREAMABLE_EVENTS`` so non-risk emits
    cost nothing.
    """
    if event_type not in STREAMABLE_EVENTS:
        return
    pending: list[dict[str, Any]] = session.info.setdefault(_PENDING_KEY, [])
    pending.append(
        {"user_id": user_id, "event_type": event_type, "payload": payload or {}, "message": message}
    )


@event.listens_for(Session, "after_commit")
def _publish_pending_after_commit(session: Session) -> None:
    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for evt in pending:
        loop.create_task(publish_user_event(**evt))


@event.listens_for(Session, "after_rollback")
def _drop_pending_after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


async def subscribe_user_events(user_id: int) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded streamable events for one user from Redis pub/sub."""
    async with _get_redis_client() as redis:
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(user_channel(user_id))
            async for message in pubsub.listen():
                if not message or message.get("type") != "message":
                    continue
                raw = message["data"]
                try:
                    yield json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                except Exception:
                    logger.exception("Failed to decode SSE event for user_id=%s", user_id)
                    continue
        finally:
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
