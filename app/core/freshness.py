"""Shared data-freshness helpers (W8).

Single source of truth for the age / staleness logic used by both the
auto-trade ai_overlay resolver (per-request freshness gate) and the scheduled
4h freshness sweep. ``now`` is always passed in so callers control the clock
(testable, and one timestamp per evaluation).
"""

from __future__ import annotations

from datetime import UTC, datetime


def normalize_to_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime; pass through aware or ``None`` unchanged.

    Timestamps stored via ``DateTime(timezone=True)`` come back naive on SQLite
    (and occasionally elsewhere); treating naive values as UTC keeps comparisons
    consistent across dialects.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def age_minutes(reference_time: datetime | None, *, now: datetime) -> float | None:
    """Minutes between ``reference_time`` (naive treated as UTC) and ``now``.

    Returns ``None`` when there is no reference time.
    """
    reference = normalize_to_utc(reference_time)
    if reference is None:
        return None
    return (now - reference).total_seconds() / 60.0


def is_fresh(reference_time: datetime | None, *, max_age_minutes: float, now: datetime) -> bool:
    """True when ``reference_time`` is within ``max_age_minutes`` of ``now``.

    The bound is inclusive (age == max ⇒ still fresh). A missing reference is
    never fresh.
    """
    age = age_minutes(reference_time, now=now)
    return age is not None and age <= max_age_minutes
