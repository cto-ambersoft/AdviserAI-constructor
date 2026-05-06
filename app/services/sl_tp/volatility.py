"""Volatility-based stop-loss adjustment."""

from __future__ import annotations

from typing import Optional

from app.services.position.context import PositionContext, PositionSide
from app.services.sl_tp.trailing import SLAdjustmentResult


def evaluate_volatility(
    position: PositionContext,
    current_atr: Optional[float],
) -> Optional[SLAdjustmentResult]:
    """Evaluate volatility-based SL update using ATR distance."""
    if not position.volatility_sl_enabled or current_atr is None:
        return None

    distance = current_atr * position.volatility_atr_multiplier

    if position.side == PositionSide.LONG:
        new_sl = position.entry_price - distance
        if new_sl <= position.current_sl_price:
            return None
    elif position.side == PositionSide.SHORT:
        new_sl = position.entry_price + distance
        if new_sl >= position.current_sl_price:
            return None
    else:
        return None

    return SLAdjustmentResult(
        new_sl_price=new_sl,
        reason="volatility",
        detail=f"ATR={current_atr:.2f}, mult={position.volatility_atr_multiplier}",
        is_valid=True,
        update_tracking={"volatility_last_atr": current_atr},
    )
