"""Process-wide audit hook for auto-trade observability.

Lower-level subsystems (``OrderExecutionQueue``, ``WebSocketManager``, ``MultiTPEngine``,
``RealtimeSLAdjuster``) need to emit ``auto_trade_event`` rows for observability,
but they do not own a DB session. This module holds a single global async hook
that ``AutoTradeService`` registers at startup; subsystems call ``emit`` to fire
audit events without taking a hard dependency on the service.

If the hook is not registered (e.g. in unit tests), emissions are dropped after
a single debug log — never raised — so safety-critical code paths never break
on missing audit infrastructure.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

AuditHook = Callable[[str, dict[str, Any]], Awaitable[None]]
_hook: AuditHook | None = None


def set_audit_hook(hook: AuditHook | None) -> None:
    """Register or clear the global audit hook."""
    global _hook
    _hook = hook


async def emit(event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort audit emit. Never raises, never blocks for long."""
    if _hook is None:
        logger.debug("audit.emit (no hook) %s %s", event_type, payload)
        return
    try:
        await _hook(event_type, payload)
    except Exception:
        logger.exception("audit.emit hook for %s failed", event_type)


__all__ = ["AuditHook", "set_audit_hook", "emit"]
