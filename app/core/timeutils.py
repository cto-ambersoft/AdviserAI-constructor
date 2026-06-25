"""Small datetime helpers shared across services."""

from datetime import UTC, datetime


def as_aware_utc(value: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime to timezone-aware UTC.

    SQLite round-trips ``DateTime(timezone=True)`` columns as naive; the stored value
    is UTC, so a naive datetime is treated as UTC. ``None`` passes through unchanged,
    and an already-aware datetime is returned as-is.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
