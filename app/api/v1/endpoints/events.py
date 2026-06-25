"""Server-Sent Events stream for live risk/governance events (B3)."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from sse_starlette import EventSourceResponse

from app.api.deps import CurrentUser
from app.core.config import get_settings
from app.services.events.stream import subscribe_user_events

router = APIRouter()

# Per-user concurrent stream count, per worker process (S1). In-process is enough:
# each SSE connection is pinned to the worker that holds it.
_active_streams: dict[int, int] = defaultdict(int)


def _max_streams_per_user() -> int:
    return get_settings().sse_max_streams_per_user


@router.get("/events/stream", summary="Live risk/governance event stream (SSE)")
async def stream_events(request: Request, current_user: CurrentUser) -> EventSourceResponse:
    """Push the authenticated user's streamable events (kill-switch, KPI-guard,
    portfolio-DD halt, data-stale, …) as Server-Sent Events.

    Note: browser ``EventSource`` cannot set an Authorization header, so the
    frontend passes the bearer token via the proxy/cookie layer. The generator
    stops cleanly on client disconnect and on server shutdown (CancelledError).
    """
    user_id = current_user.id
    if _active_streams[user_id] >= _max_streams_per_user():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many concurrent event streams.",
        )
    _active_streams[user_id] += 1

    async def _generator() -> AsyncIterator[dict[str, str]]:
        try:
            async for event in subscribe_user_events(user_id):
                if await request.is_disconnected():
                    break
                yield {
                    "event": str(event.get("event_type", "message")),
                    "data": json.dumps(event, separators=(",", ":"), ensure_ascii=False),
                }
        except asyncio.CancelledError:
            # Client disconnect / server shutdown — let it propagate so sse-starlette
            # can tear the stream down cleanly.
            raise
        finally:
            _active_streams[user_id] -= 1
            if _active_streams[user_id] <= 0:
                _active_streams.pop(user_id, None)

    return EventSourceResponse(_generator(), ping=15)
