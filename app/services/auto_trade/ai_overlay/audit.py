"""Audit payload builder for ai-overlay events.

Writing rows into ``auto_trade_events`` is owned by the existing
``AutoTradeService._emit_event`` helper; this module only standardises
the ``event_type`` constants and the JSON payload shape so consumers
(dashboards, tests) can rely on a stable schema.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.schemas.ai_overlay import AiTrendSnapshot

#: How many top-weighted agent entries to denormalise into the audit payload.
#: Six is enough to communicate the "shape" of the decision without bloating
#: every audit row. Full reasoning remains available in core via decisionEventId.
_REASONING_TOP_N = 6


class AiOverlayEventType(StrEnum):
    BLOCK_ENTRY = "ai_overlay_block_entry"
    ATR_SCALED = "ai_overlay_atr_scaled"
    RSI_SCALED = "ai_overlay_rsi_scaled"
    STALE_FALLBACK = "ai_overlay_stale_fallback"


def _select_top_reasoning(
    snapshot: AiTrendSnapshot,
    *,
    limit: int = _REASONING_TOP_N,
) -> list[dict[str, Any]]:
    """Return the highest-weighted ``limit`` reasoning entries as compact dicts.

    Sorting uses ``weight`` as primary (descending) then ``confidence`` so
    that ties are broken predictably. Entries with no weight fall to the
    bottom but stay in the result if there's room — they still carry
    qualitative info like ``summary``.
    """
    if not snapshot.reasoning_path:
        return []
    sorted_entries = sorted(
        snapshot.reasoning_path,
        key=lambda e: (
            -(e.weight if e.weight is not None else float("-inf")),
            -(e.confidence if e.confidence is not None else float("-inf")),
        ),
    )
    return [entry.to_compact_payload() for entry in sorted_entries[:limit]]


def build_overlay_payload(
    *,
    event_type: AiOverlayEventType,
    reason: str,
    snapshot: AiTrendSnapshot | None,
    before: Any = None,
    after: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the JSON payload stored on the AutoTradeEvent row.

    Keeping ``before``/``after`` typed as ``Any`` lets callers pass scalars
    (ATR multiplier float) or tuples (RSI thresholds) without coercion.

    When ``snapshot`` carries W2-traceability fields (``decision_event_id``,
    ``reasoning_path``) they are denormalised onto the payload so downstream
    consumers (UI, audit replays) can read them without a round-trip to
    core's ``ai_decision_events`` collection.
    """
    payload: dict[str, Any] = {
        "overlay_event": event_type.value,
        "reason": reason,
    }
    if snapshot is not None:
        ai_trend_block: dict[str, Any] = {
            "direction": snapshot.direction,
            "strength": snapshot.strength,
            "occurred_at": snapshot.occurred_at_iso,
            "source": snapshot.source,
        }
        if snapshot.decision_event_id:
            ai_trend_block["decision_event_id"] = snapshot.decision_event_id
        payload["ai_trend"] = ai_trend_block
        reasoning = _select_top_reasoning(snapshot)
        if reasoning:
            payload["reasoning_path"] = reasoning
    if before is not None:
        payload["before"] = before
    if after is not None:
        payload["after"] = after
    if extra:
        payload.update(extra)
    return payload
