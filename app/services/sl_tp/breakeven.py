"""Breakeven adjuster."""

from __future__ import annotations

from typing import Optional

from app.services.position.context import PositionContext, PositionSide
from app.services.sl_tp.trailing import SLAdjustmentResult


def evaluate_breakeven(
    position: PositionContext,
    current_price: float,
) -> Optional[SLAdjustmentResult]:
    """Evaluate one-time breakeven SL move."""
    if not position.breakeven_enabled or position.breakeven_activated:
        return None

    risk = abs(position.entry_price - position.current_sl_price)
    required_move = risk * position.breakeven_trigger_rr

    if position.side == PositionSide.LONG:
        threshold = position.entry_price + required_move
        if current_price < threshold:
            return None
    elif position.side == PositionSide.SHORT:
        threshold = position.entry_price - required_move
        if current_price > threshold:
            return None
    else:
        return None

    return SLAdjustmentResult(
        new_sl_price=position.entry_price,
        reason="breakeven",
        detail=f"R:R={position.breakeven_trigger_rr}, price={current_price:.2f}",
        is_valid=True,
        update_tracking={"breakeven_activated": True},
    )
