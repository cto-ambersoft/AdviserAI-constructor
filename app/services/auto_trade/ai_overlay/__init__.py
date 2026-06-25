"""AI Trend Overlay — runtime adaptation of auto-trade parameters from ai_trend.

W4 of Milestone 4. See ``app/schemas/ai_overlay.py`` for the config model
and ``ADVANCED_EXCHANGE_ARCH.md`` for the broader execution context.
"""

from app.services.auto_trade.ai_overlay.audit import (
    AiOverlayEventType,
    build_overlay_payload,
)
from app.services.auto_trade.ai_overlay.resolver import resolve_ai_trend
from app.services.auto_trade.ai_overlay.scaler import (
    OverlayDecision,
    scale_atr_multiplier,
    scale_rsi_thresholds,
    shift_watcher_condition_threshold,
    should_block_entry,
)

__all__ = [
    "AiOverlayEventType",
    "OverlayDecision",
    "build_overlay_payload",
    "resolve_ai_trend",
    "scale_atr_multiplier",
    "scale_rsi_thresholds",
    "shift_watcher_condition_threshold",
    "should_block_entry",
]
