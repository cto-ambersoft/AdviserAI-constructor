"""Unit tests for volatility-based SL adjustment."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.position.context import PositionContext, PositionSide  # noqa: E402
from app.services.sl_tp.volatility import evaluate_volatility  # noqa: E402


def test_evaluate_volatility_long_initial_tightening_is_valid() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=95000.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
    )

    result = evaluate_volatility(position, current_atr=1500.0)

    assert result is not None
    assert result.is_valid is True
    assert result.reason == "volatility"
    assert result.new_sl_price == pytest.approx(97000.0)
    assert result.update_tracking == {"volatility_last_atr": pytest.approx(1500.0)}


def test_evaluate_volatility_long_atr_shrinks_tightens_further() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=97000.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
    )

    result = evaluate_volatility(position, current_atr=1000.0)

    assert result is not None
    assert result.new_sl_price == pytest.approx(98000.0)
    assert result.update_tracking == {"volatility_last_atr": pytest.approx(1000.0)}


def test_evaluate_volatility_long_atr_expands_and_widening_is_rejected() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=97000.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
    )

    result = evaluate_volatility(position, current_atr=3000.0)

    assert result is None


def test_evaluate_volatility_short_initial_tightening_is_valid() -> None:
    position = PositionContext(
        side=PositionSide.SHORT,
        entry_price=100000.0,
        current_sl_price=105000.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
    )

    result = evaluate_volatility(position, current_atr=1500.0)

    assert result is not None
    assert result.is_valid is True
    assert result.reason == "volatility"
    assert result.new_sl_price == pytest.approx(103000.0)
    assert result.update_tracking == {"volatility_last_atr": pytest.approx(1500.0)}


def test_evaluate_volatility_returns_none_when_atr_is_missing() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=95000.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
    )

    result = evaluate_volatility(position, current_atr=None)

    assert result is None


def test_evaluate_volatility_returns_none_when_disabled() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=95000.0,
        volatility_sl_enabled=False,
        volatility_atr_multiplier=2.0,
    )

    result = evaluate_volatility(position, current_atr=1500.0)

    assert result is None
