"""Unit tests for trailing stop evaluation."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.position.context import PositionContext, PositionSide  # noqa: E402
from app.services.sl_tp.trailing import evaluate_trailing  # noqa: E402


def test_evaluate_trailing_long_initial_move_updates_high_and_sl() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
    )

    result = evaluate_trailing(position, current_price=102000.0)

    assert result is not None
    assert result.is_valid is True
    assert result.reason == "trailing"
    assert result.new_sl_price == pytest.approx(100980.0)
    assert result.update_tracking == {"trailing_highest_price": pytest.approx(102000.0)}


def test_evaluate_trailing_long_drop_keeps_previous_high_and_no_sl_update() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=100980.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
        trailing_highest_price=102000.0,
    )

    result = evaluate_trailing(position, current_price=101000.0)

    assert result is None


def test_evaluate_trailing_long_new_high_moves_sl_higher() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=100980.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
        trailing_highest_price=102000.0,
    )

    result = evaluate_trailing(position, current_price=105000.0)

    assert result is not None
    assert result.new_sl_price == pytest.approx(103950.0)
    assert result.update_tracking == {"trailing_highest_price": pytest.approx(105000.0)}


def test_evaluate_trailing_short_initial_move_updates_low_and_sl() -> None:
    position = PositionContext(
        side=PositionSide.SHORT,
        entry_price=100000.0,
        current_sl_price=102000.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
    )

    result = evaluate_trailing(position, current_price=98000.0)

    assert result is not None
    assert result.is_valid is True
    assert result.reason == "trailing"
    assert result.new_sl_price == pytest.approx(98980.0)
    assert result.update_tracking == {"trailing_lowest_price": pytest.approx(98000.0)}


def test_evaluate_trailing_returns_none_when_disabled() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        trailing_enabled=False,
        trailing_callback_rate=1.0,
    )

    result = evaluate_trailing(position, current_price=102000.0)

    assert result is None


def test_evaluate_trailing_long_does_not_move_sl_backward() -> None:
    tracked_high = 101_515.15151515152
    expected_new_sl = tracked_high * (1 - 1 / 100)
    assert expected_new_sl == pytest.approx(100500.0)

    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=101000.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
        trailing_highest_price=tracked_high,
    )

    result = evaluate_trailing(position, current_price=100000.0)

    assert result is None
