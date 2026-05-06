"""Unit tests for generic watcher condition parser."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.watchers.rule_engine import evaluate_condition  # noqa: E402


@pytest.mark.parametrize(
    ("condition", "value", "expected"),
    [
        ("> 75", 76.5, True),
        ("> 75", 74.0, False),
        ("< 30", 28.0, True),
        (">= 100", 100.0, True),
        ("< 0.5", 0.3, True),
        ("> 1500", 2000.0, True),
        (
            "cross_below",
            {"line": -5, "signal": -3, "prev_line": -2, "prev_signal": -3},
            True,
        ),
        (
            "cross_below",
            {"line": -5, "signal": -3, "prev_line": -5, "prev_signal": -3},
            False,
        ),
        (
            "cross_above",
            {"line": 5, "signal": 3, "prev_line": 2, "prev_signal": 3},
            True,
        ),
        ("between 30 70", 50, True),
        ("between 30 70", 75, False),
        ("outside 30 70", 25, True),
        ("UNKNOWN_PATTERN", 50, False),
        ("> 75", None, False),
        ("> 75", "abc", False),
        ("cross_below", {"line": 5}, False),
    ],
)
def test_evaluate_condition(condition: str, value: Any, expected: bool) -> None:
    assert evaluate_condition(condition, value) is expected


def test_evaluate_condition_logs_warning_for_invalid_pattern(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.watchers.rule_engine"):
        result = evaluate_condition("UNKNOWN_PATTERN", 42.0)

    assert result is False
    assert "Invalid watcher condition pattern: UNKNOWN_PATTERN" in caplog.text
