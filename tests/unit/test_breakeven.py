"""Unit tests for breakeven stop-loss adjustment."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.position.context import PositionContext, PositionSide  # noqa: E402
from app.services.sl_tp.breakeven import evaluate_breakeven  # noqa: E402


def test_evaluate_breakeven_long_threshold_hit_moves_sl_to_entry() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
    )

    result = evaluate_breakeven(position, current_price=102000.0)

    assert result is not None
    assert result.is_valid is True
    assert result.reason == "breakeven"
    assert result.new_sl_price == pytest.approx(100000.0)
    assert result.update_tracking == {"breakeven_activated": True}


def test_evaluate_breakeven_long_below_threshold_returns_none() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
    )

    result = evaluate_breakeven(position, current_price=101999.0)

    assert result is None


def test_evaluate_breakeven_when_already_activated_returns_none() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
        breakeven_activated=True,
    )

    result = evaluate_breakeven(position, current_price=102000.0)

    assert result is None


def test_evaluate_breakeven_respects_custom_trigger_rr() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.5,
    )

    result = evaluate_breakeven(position, current_price=103000.0)

    assert result is not None
    assert result.new_sl_price == pytest.approx(100000.0)
    assert result.update_tracking == {"breakeven_activated": True}


def test_evaluate_breakeven_short_threshold_hit_moves_sl_to_entry() -> None:
    position = PositionContext(
        side=PositionSide.SHORT,
        entry_price=100000.0,
        current_sl_price=102000.0,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
    )

    result = evaluate_breakeven(position, current_price=98000.0)

    assert result is not None
    assert result.is_valid is True
    assert result.reason == "breakeven"
    assert result.new_sl_price == pytest.approx(100000.0)
    assert result.update_tracking == {"breakeven_activated": True}


def test_evaluate_breakeven_returns_none_when_disabled() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        breakeven_enabled=False,
        breakeven_trigger_rr=1.0,
    )

    result = evaluate_breakeven(position, current_price=102000.0)

    assert result is None
