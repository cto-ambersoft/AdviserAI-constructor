"""Generic watcher condition evaluator."""

from __future__ import annotations

import logging
import operator
import re
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

_COMPARISON_RE = re.compile(r"^([><]=?)\s*(-?[\d.]+)$")
_RANGE_RE = re.compile(r"^(between|outside)\s+(-?[\d.]+)\s+(-?[\d.]+)$", re.IGNORECASE)
_CROSS_KEYS = ("line", "signal", "prev_line", "prev_signal")
_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _evaluate_comparison(operator_symbol: str, threshold_raw: str, value: Any) -> bool:
    comparator = _COMPARATORS.get(operator_symbol)
    if comparator is None:
        return False

    current = _to_float(value)
    threshold = _to_float(threshold_raw)
    if current is None or threshold is None:
        return False
    return comparator(current, threshold)


def _evaluate_cross(condition: str, value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if any(key not in value for key in _CROSS_KEYS):
        return False

    line = _to_float(value["line"])
    signal = _to_float(value["signal"])
    prev_line = _to_float(value["prev_line"])
    prev_signal = _to_float(value["prev_signal"])
    if line is None or signal is None or prev_line is None or prev_signal is None:
        return False

    if condition == "cross_below":
        return line < signal and prev_line >= prev_signal
    return line > signal and prev_line <= prev_signal


def _evaluate_range(kind: str, lower_raw: str, upper_raw: str, value: Any) -> bool:
    current = _to_float(value)
    lower = _to_float(lower_raw)
    upper = _to_float(upper_raw)
    if current is None or lower is None or upper is None:
        return False
    if lower > upper:
        return False

    if kind == "between":
        return lower <= current <= upper
    return current < lower or current > upper


def evaluate_condition(condition: str, value: Any) -> bool:
    """Evaluate a generic watcher condition against a computed indicator value."""
    if value is None:
        return False

    normalized = " ".join(condition.strip().split()).lower()
    if not normalized:
        logger.warning("Invalid empty watcher condition: %r", condition)
        return False

    if normalized in {"cross_below", "cross_above"}:
        return _evaluate_cross(normalized, value)

    comparison_match = _COMPARISON_RE.fullmatch(normalized)
    if comparison_match is not None:
        operator_symbol, threshold_raw = comparison_match.groups()
        return _evaluate_comparison(operator_symbol, threshold_raw, value)

    range_match = _RANGE_RE.fullmatch(normalized)
    if range_match is not None:
        range_kind, lower_raw, upper_raw = range_match.groups()
        return _evaluate_range(range_kind.lower(), lower_raw, upper_raw, value)

    logger.warning("Invalid watcher condition pattern: %s", condition)
    return False
