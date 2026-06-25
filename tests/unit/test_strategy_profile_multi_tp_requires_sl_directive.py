"""Reject ambiguous multi-TP profiles at strategy save time.

Phase 3.15 of the SL repositioning fix: multi-TP profiles must declare an
explicit SL directive (``sl_lock_pct`` or ``move_sl_to``, with the literal
string ``'none'`` meaning "keep SL fixed") on every level except the last.
Leaving both null silently skipped SL movement at runtime — the exact failure
mode users had hit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from app.schemas.strategy_profile import (  # noqa: E402
    StrategyProfileConfig,
    StrategyProfileTPLevel,
)


def _build(levels: list[dict[str, object]]) -> dict[str, object]:
    return {
        "sl_mode": "fixed",
        "sl_value": 1.0,
        "tp_mode": "multi",
        "tp_levels": levels,
    }


def test_multi_tp_with_all_explicit_sl_directives_passes() -> None:
    payload = _build(
        [
            {"price_offset_pct": 1.0, "close_pct": 50.0, "sl_lock_pct": 0.0},
            {"price_offset_pct": 3.0, "close_pct": 50.0},
        ]
    )
    config = StrategyProfileConfig.model_validate(payload)
    assert config.tp_levels is not None
    assert config.tp_levels[0].sl_lock_pct == pytest.approx(0.0)


def test_multi_tp_with_explicit_none_opt_out_passes() -> None:
    payload = _build(
        [
            {
                "price_offset_pct": 1.0,
                "close_pct": 50.0,
                "move_sl_to": "none",
            },
            {"price_offset_pct": 3.0, "close_pct": 50.0},
        ]
    )
    config = StrategyProfileConfig.model_validate(payload)
    assert config.tp_levels is not None
    assert config.tp_levels[0].move_sl_to == "none"


def test_multi_tp_with_no_sl_directive_on_first_level_is_rejected() -> None:
    payload = _build(
        [
            {"price_offset_pct": 1.0, "close_pct": 50.0},  # neither directive
            {"price_offset_pct": 3.0, "close_pct": 50.0},
        ]
    )
    with pytest.raises(ValidationError) as exc_info:
        StrategyProfileConfig.model_validate(payload)
    error_text = str(exc_info.value)
    assert "level 1" in error_text or "Multi-TP level 1" in error_text


def test_multi_tp_last_level_without_directive_still_passes() -> None:
    """Only non-final levels need a directive; the final level just closes the rest."""
    payload = _build(
        [
            {
                "price_offset_pct": 1.0,
                "close_pct": 33.0,
                "sl_lock_pct": 0.0,
            },
            {
                "price_offset_pct": 2.0,
                "close_pct": 33.0,
                "move_sl_to": "tp1",
            },
            {"price_offset_pct": 3.0, "close_pct": 34.0},  # final level OK
        ]
    )
    config = StrategyProfileConfig.model_validate(payload)
    assert config.tp_levels is not None
    assert len(config.tp_levels) == 3


def test_tp_level_invalid_move_sl_to_string_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyProfileTPLevel.model_validate(
            {
                "price_offset_pct": 1.0,
                "close_pct": 50.0,
                "move_sl_to": "magic",
            }
        )
