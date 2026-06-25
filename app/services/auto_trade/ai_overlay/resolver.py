"""Read the freshest ai_trend datapoint from local PersonalAnalysisHistory.

The resolver intentionally keeps no HTTP path in the auto-trade hot loop —
it only reads what the existing personal-analysis pipeline already
populates. If the latest record is missing or stale the resolver returns
``None`` and the caller falls back to static behaviour (fail-open).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.freshness import is_fresh, normalize_to_utc
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.schemas.ai_overlay import AiReasoningEntry, AiTrendDirection, AiTrendSnapshot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_direction(value: Any) -> AiTrendDirection | None:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("up", "down", "flat"):
            return cast(AiTrendDirection, lowered)
    return None


def _coerce_strength(value: Any) -> float | None:
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    if as_float < 0.0 or as_float > 1.0:
        return None
    return as_float


def _unwrap_envelope(analysis_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the inner dict that holds ``aiTrend``/``reasoningPath``/``decisionEventId``.

    Core's response may arrive flat or nested under ``result_json`` / ``result``.
    We prefer the form whose ``aiTrend`` field is a dict so the rest of the
    resolver can read its keys directly.
    """
    if not analysis_data:
        return None
    if isinstance(analysis_data.get("aiTrend"), dict) or isinstance(
        analysis_data.get("ai_trend"), dict
    ):
        return analysis_data
    nested = analysis_data.get("result_json") or analysis_data.get("result")
    if isinstance(nested, dict) and (
        isinstance(nested.get("aiTrend"), dict)
        or isinstance(nested.get("ai_trend"), dict)
    ):
        return nested
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coerce_optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_reasoning_path(envelope: dict[str, Any]) -> list[AiReasoningEntry]:
    """Pull per-agent reasoning entries from the envelope, tolerant to gaps."""
    raw = envelope.get("reasoningPath") or envelope.get("reasoning_path") or []
    if not isinstance(raw, list):
        return []
    entries: list[AiReasoningEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        agent_key = _coerce_str(item.get("agentKey") or item.get("agent_key"))
        signal_value = _coerce_str(item.get("signal"))
        # Confidence/weight may legitimately be 0.0, so we must accept those.
        confidence_value = _coerce_optional_float(item.get("confidence"))
        weight_value = _coerce_optional_float(item.get("weight"))
        summary_value = _coerce_str(item.get("summary"))
        # If literally every field is None the entry is useless — skip.
        if (
            agent_key is None
            and signal_value is None
            and confidence_value is None
            and weight_value is None
            and summary_value is None
        ):
            continue
        entries.append(
            AiReasoningEntry(
                agent_key=agent_key,
                signal=signal_value,
                confidence=confidence_value,
                weight=weight_value,
                summary=summary_value,
            )
        )
    return entries


def _extract_ai_trend(
    analysis_data: dict[str, Any] | None,
) -> tuple[str, float, str | None, list[AiReasoningEntry]] | None:
    """Walk the analysis_data envelope and pull all overlay-relevant fields.

    Returns ``(direction, strength, decision_event_id, reasoning_path)`` or
    None when the envelope lacks a usable ai_trend. The optional fields
    ``decision_event_id`` and ``reasoning_path`` provide W2 traceability
    but are gracefully absent for older records.
    """
    envelope = _unwrap_envelope(analysis_data)
    if envelope is None:
        return None

    candidate = envelope.get("aiTrend") or envelope.get("ai_trend")
    if not isinstance(candidate, dict):
        return None

    direction = _coerce_direction(candidate.get("direction"))
    strength = _coerce_strength(candidate.get("strength"))
    if direction is None or strength is None:
        return None

    decision_event_id = _coerce_str(
        envelope.get("decisionEventId") or envelope.get("decision_event_id")
    )
    reasoning_path = _extract_reasoning_path(envelope)
    return direction, strength, decision_event_id, reasoning_path


async def resolve_ai_trend(
    *,
    session: AsyncSession,
    user_id: int,
    symbol: str,
    max_age_minutes: int,
    profile_id: int | None = None,
) -> AiTrendSnapshot | None:
    """Return the freshest valid ai_trend for ``(user_id, symbol)`` or None.

    The query is one indexed lookup against ``personal_analysis_history``
    (the same table the personal-analysis pipeline writes to). The
    ``profile_id`` filter is optional — when present it scopes the query
    to a specific user-defined profile, otherwise the most recent record
    for the user/symbol wins.
    """
    stmt = (
        select(PersonalAnalysisHistory)
        .where(
            PersonalAnalysisHistory.user_id == user_id,
            PersonalAnalysisHistory.symbol == symbol,
        )
        .order_by(PersonalAnalysisHistory.created_at.desc())
        .limit(1)
    )
    if profile_id is not None:
        stmt = stmt.where(PersonalAnalysisHistory.profile_id == profile_id)

    record = await session.scalar(stmt)
    if record is None:
        return None

    extracted = _extract_ai_trend(record.analysis_data)
    if extracted is None:
        return None
    direction, strength, decision_event_id, reasoning_path = extracted

    # Prefer the upstream ``core_completed_at`` timestamp (when ai_trend was
    # actually computed) and fall back to the local insert time.
    reference_time = normalize_to_utc(record.core_completed_at or record.created_at)
    if reference_time is None:
        return None
    if not is_fresh(reference_time, max_age_minutes=max_age_minutes, now=_utc_now()):
        return None

    return AiTrendSnapshot(
        direction=cast(AiTrendDirection, direction),
        strength=strength,
        occurred_at_iso=reference_time.isoformat(),
        source="personal_analysis_history",
        decision_event_id=decision_event_id,
        reasoning_path=reasoning_path,
    )
