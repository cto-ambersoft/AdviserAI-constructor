"""SL adjustment pipeline."""

from __future__ import annotations

from typing import Any, Optional

from app.services.position.context import PositionContext, PositionSide
from app.services.sl_tp.breakeven import evaluate_breakeven
from app.services.sl_tp.trailing import SLAdjustmentResult, evaluate_trailing
from app.services.sl_tp.volatility import evaluate_volatility


class SLAdjustmentPipeline:
    """Evaluate SL adjustment sources and pick the most protective candidate."""

    def __init__(self, position: PositionContext) -> None:
        self.position = position

    async def evaluate(
        self,
        current_price: float,
        indicators: dict[str, Any],
        kline_data: list[Any],
    ) -> Optional[SLAdjustmentResult]:
        """Run adjustment sources and return the strongest valid result."""
        del kline_data  # Reserved for future watcher/trailing extensions.

        candidates: list[SLAdjustmentResult] = []
        for source in self.position.adjustment_priority:
            result = await self._evaluate_source(
                source=source,
                current_price=current_price,
                indicators=indicators,
            )
            if result is not None and result.is_valid:
                candidates.append(result)

        if not candidates:
            return None

        if self.position.side == PositionSide.SHORT:
            return min(candidates, key=lambda item: item.new_sl_price)
        return max(candidates, key=lambda item: item.new_sl_price)

    async def _evaluate_source(
        self,
        source: str,
        current_price: float,
        indicators: dict[str, Any],
    ) -> Optional[SLAdjustmentResult]:
        if source == "watcher":
            # Watcher-triggered SL moves are applied via the event bus
            # (indicator_watcher → Redis → the runtime's watcher-event consumer →
            # handle_watcher_event), NOT this priority pipeline. The slot is a
            # deliberate no-op so a config that lists "watcher" in its
            # adjustment_priority resolves cleanly instead of erroring.
            return None
        if source == "trailing":
            return evaluate_trailing(self.position, current_price)
        if source == "breakeven":
            return evaluate_breakeven(self.position, current_price)
        if source == "volatility":
            return evaluate_volatility(self.position, indicators.get("ATR"))
        return None
