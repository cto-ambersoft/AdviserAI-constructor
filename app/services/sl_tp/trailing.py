"""Trailing stop evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.services.position.context import PositionContext, PositionSide


@dataclass
class SLAdjustmentResult:
    """Result of stop-loss adjustment calculation."""

    new_sl_price: float
    reason: str
    detail: str
    is_valid: bool
    update_tracking: dict[str, float | bool]


def evaluate_trailing(
    position: PositionContext,
    current_price: float,
) -> Optional[SLAdjustmentResult]:
    """Evaluate trailing stop adjustment for current price tick.

    Implements classic trailing logic:
    - LONG: track highest price, SL = highest * (1 - callback_rate/100)
    - SHORT: track lowest price, SL = lowest * (1 + callback_rate/100)

    SL is moved only if the new value is more protective.
    """
    if not position.trailing_enabled:
        return None

    callback_rate = position.trailing_callback_rate
    if callback_rate is None or callback_rate <= 0:
        return None

    if position.side == PositionSide.LONG:
        new_high = max(position.trailing_highest_price or position.entry_price, current_price)
        new_sl = new_high * (1 - callback_rate / 100)
        if new_sl <= position.current_sl_price:
            return None
        return SLAdjustmentResult(
            new_sl_price=new_sl,
            reason="trailing",
            detail=f"peak={new_high:.2f}, callback={callback_rate}%",
            is_valid=True,
            update_tracking={"trailing_highest_price": new_high},
        )

    if position.side == PositionSide.SHORT:
        new_low = min(position.trailing_lowest_price or position.entry_price, current_price)
        new_sl = new_low * (1 + callback_rate / 100)
        if new_sl >= position.current_sl_price:
            return None
        return SLAdjustmentResult(
            new_sl_price=new_sl,
            reason="trailing",
            detail=f"trough={new_low:.2f}, callback={callback_rate}%",
            is_valid=True,
            update_tracking={"trailing_lowest_price": new_low},
        )

    return None
