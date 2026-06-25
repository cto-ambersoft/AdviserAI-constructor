"""Schema-validator tests for AutoTradeConfigUpsertRequest.

Regression coverage for the percent/fraction unit-mismatch defect: a
``min_confidence_pct`` of 0.65 used to silently mean "0.65 %" while
operators meant "65 %", disabling the entry gate at
``service.py:_process_without_open_position`` for every realistic signal.

These tests pin the validator behaviour so a future schema relaxation
that re-opens the (0, 1) range will fail loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas.auto_trade import AutoTradeConfigUpsertRequest  # noqa: E402


def _base_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enabled": True,
        "profile_id": 1,
        "account_id": 1,
        "position_size_usdt": 100.0,
        "leverage": 10,
        "min_confidence_pct": 65.0,
        "fast_close_confidence_pct": 80.0,
        "confirm_reports_required": 2,
        "risk_mode": "1:2",
        "sl_pct": 1.0,
        "tp_pct": 2.0,
    }
    payload.update(overrides)
    return payload


def test_fractional_min_confidence_pct_is_rejected_with_helpful_hint() -> None:
    """0.65 → ValueError with "Did you mean 65?" hint.

    Pydantic surfaces ``Field(ge=1)`` violations directly; the
    model-level validator adds the unit-conversion suggestion.
    """
    with pytest.raises(ValidationError) as exc_info:
        AutoTradeConfigUpsertRequest(**_base_payload(min_confidence_pct=0.65))
    message = str(exc_info.value)
    assert "min_confidence_pct" in message
    # Either pydantic's "greater than or equal to 1" or the validator's hint.
    assert "1" in message or "65" in message


def test_fractional_fast_close_confidence_pct_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AutoTradeConfigUpsertRequest(
            **_base_payload(min_confidence_pct=10.0, fast_close_confidence_pct=0.8),
        )
    message = str(exc_info.value)
    assert "fast_close_confidence_pct" in message


def test_zero_confidence_pct_is_rejected() -> None:
    """Zero is also rejected — historically a sentinel that signals
    "no minimum confidence" but in practice always a misconfiguration
    that disables the gate."""
    with pytest.raises(ValidationError):
        AutoTradeConfigUpsertRequest(**_base_payload(min_confidence_pct=0))


def test_whole_percent_min_confidence_is_accepted() -> None:
    cfg = AutoTradeConfigUpsertRequest(**_base_payload(min_confidence_pct=65.0))
    assert cfg.min_confidence_pct == 65.0


def test_one_percent_lower_bound_is_accepted() -> None:
    """The new lower bound (1.0) is inclusive — useful for stress tests
    even though no production strategy would use a 1 % threshold."""
    cfg = AutoTradeConfigUpsertRequest(
        **_base_payload(min_confidence_pct=1.0, fast_close_confidence_pct=1.0),
    )
    assert cfg.min_confidence_pct == 1.0
    assert cfg.fast_close_confidence_pct == 1.0


def test_hundred_percent_upper_bound_is_accepted() -> None:
    cfg = AutoTradeConfigUpsertRequest(
        **_base_payload(min_confidence_pct=100.0, fast_close_confidence_pct=100.0),
    )
    assert cfg.min_confidence_pct == 100.0
    assert cfg.fast_close_confidence_pct == 100.0


def test_fast_close_below_min_still_rejected_post_unit_fix() -> None:
    """The pre-existing ``fast_close >= min`` invariant must still hold.

    Both values are in percent units now, so the comparison is meaningful.
    """
    with pytest.raises(ValidationError) as exc_info:
        AutoTradeConfigUpsertRequest(
            **_base_payload(min_confidence_pct=70.0, fast_close_confidence_pct=60.0),
        )
    assert "fast_close_confidence_pct" in str(exc_info.value)
