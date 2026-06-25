"""Shared UTC-coercion helper (S2 — was duplicated in portfolio.py + notifications)."""

from datetime import UTC, datetime

from app.core.timeutils import as_aware_utc


def test_naive_is_treated_as_utc() -> None:
    naive = datetime(2026, 6, 1, 12, 0, 0)
    out = as_aware_utc(naive)
    assert out is not None
    assert out.tzinfo is UTC
    assert out == naive.replace(tzinfo=UTC)


def test_aware_is_returned_unchanged() -> None:
    aware = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert as_aware_utc(aware) is aware


def test_none_passes_through() -> None:
    assert as_aware_utc(None) is None
