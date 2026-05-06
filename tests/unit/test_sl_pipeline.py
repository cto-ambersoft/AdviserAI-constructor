"""Unit tests for SL adjustment pipeline."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.position.context import PositionContext, PositionSide  # noqa: E402
from app.services.sl_tp.pipeline import SLAdjustmentPipeline  # noqa: E402


@pytest.mark.asyncio
async def test_pipeline_long_picks_most_protective_from_all_sources() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
        trailing_highest_price=102020.20202020202,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
        adjustment_priority=["watcher", "trailing", "breakeven", "volatility"],
    )
    pipeline = SLAdjustmentPipeline(position)

    result = await pipeline.evaluate(
        current_price=102000.0,
        indicators={"ATR": 500.0},
        kline_data=[],
    )

    assert result is not None
    assert result.reason == "trailing"
    assert result.new_sl_price == pytest.approx(101000.0)


@pytest.mark.asyncio
async def test_pipeline_returns_trailing_when_only_trailing_enabled() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
        breakeven_enabled=False,
        volatility_sl_enabled=False,
        adjustment_priority=["trailing", "breakeven", "volatility"],
    )
    pipeline = SLAdjustmentPipeline(position)

    result = await pipeline.evaluate(
        current_price=102000.0,
        indicators={"ATR": 500.0},
        kline_data=[],
    )

    assert result is not None
    assert result.reason == "trailing"
    assert result.new_sl_price == pytest.approx(100980.0)


@pytest.mark.asyncio
async def test_pipeline_returns_none_when_no_source_fires() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=100500.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
        trailing_highest_price=100000.0,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
        adjustment_priority=["trailing", "breakeven", "volatility"],
    )
    pipeline = SLAdjustmentPipeline(position)

    result = await pipeline.evaluate(
        current_price=100000.0,
        indicators={"ATR": 3000.0},
        kline_data=[],
    )

    assert result is None


@pytest.mark.asyncio
async def test_pipeline_short_picks_lowest_most_protective_sl() -> None:
    position = PositionContext(
        side=PositionSide.SHORT,
        entry_price=100000.0,
        current_sl_price=101950.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
        trailing_lowest_price=98019.80198019802,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
        adjustment_priority=["trailing", "breakeven", "volatility"],
    )
    pipeline = SLAdjustmentPipeline(position)

    result = await pipeline.evaluate(
        current_price=98050.0,
        indicators={"ATR": 500.0},
        kline_data=[],
    )

    assert result is not None
    assert result.reason == "trailing"
    assert result.new_sl_price == pytest.approx(99000.0)


@pytest.mark.asyncio
async def test_pipeline_priority_order_does_not_change_winner() -> None:
    position = PositionContext(
        side=PositionSide.LONG,
        entry_price=100000.0,
        current_sl_price=98000.0,
        trailing_enabled=True,
        trailing_callback_rate=1.0,
        trailing_highest_price=102020.20202020202,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
        volatility_sl_enabled=True,
        volatility_atr_multiplier=2.0,
        adjustment_priority=["volatility", "breakeven", "trailing", "watcher"],
    )
    pipeline = SLAdjustmentPipeline(position)

    result = await pipeline.evaluate(
        current_price=102000.0,
        indicators={"ATR": 500.0},
        kline_data=[],
    )

    assert result is not None
    assert result.reason == "trailing"
    assert result.new_sl_price == pytest.approx(101000.0)
