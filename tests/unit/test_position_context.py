"""Unit tests for PositionContext dataclass serialization and defaults."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.position.context import (  # noqa: E402
    PositionContext,
    PositionSide,
    SLHistoryEntry,
    SUPPORTED_WATCHER_INDICATORS,
    TPHistoryEntry,
    TPLevel,
    WatcherConfig,
)
from app.services.position.state_machine import PositionState  # noqa: E402


def test_position_context_defaults_are_created() -> None:
    context = PositionContext()

    assert context.state == PositionState.PENDING
    assert context.side == PositionSide.LONG
    assert context.state_machine.state == PositionState.PENDING
    assert context.sl_history == []
    assert context.tp_levels == []
    assert context.tp_history == []
    assert context.active_watchers == []
    assert context.adjustment_priority == ["watcher", "trailing", "breakeven", "volatility"]
    assert context.trailing_enabled is False
    assert context.breakeven_enabled is False
    assert context.volatility_sl_enabled is False


def test_position_context_round_trip_to_db_and_back() -> None:
    now = datetime.now(timezone.utc).isoformat()
    context = PositionContext(
        position_id="pos-1",
        user_id="user-1",
        account_id="acc-1",
        exchange="binance",
        symbol="BTCUSDT",
        state=PositionState.OPEN,
        side=PositionSide.LONG,
        entry_price=100.0,
        original_quantity=2.0,
        current_quantity=1.5,
        leverage=10,
        current_sl_price=95.0,
        sl_exchange_order_id="sl-1",
        sl_type="trailing",
        sl_history=[
            SLHistoryEntry(
                timestamp=now,
                old_price=90.0,
                new_price=95.0,
                reason="trailing",
                trigger_source="trailing_engine",
                exchange_order_id="sl-1",
            )
        ],
        tp_mode="multi",
        tp_levels=[
            TPLevel.from_offset(
                level=0,
                price_offset_pct=1.0,
                close_pct=25.0,
                entry_price=100.0,
                side=PositionSide.LONG,
            )
        ],
        current_tp_price=None,
        tp_history=[
            TPHistoryEntry(
                timestamp=now,
                tp_level=0,
                old_price=101.0,
                new_price=102.0,
                reason="indicator",
                close_pct=25.0,
                exchange_order_id="tp-1",
            )
        ],
        trailing_enabled=True,
        trailing_callback_rate=0.5,
        trailing_activation_price=101.0,
        trailing_highest_price=103.0,
        trailing_lowest_price=None,
        breakeven_enabled=True,
        breakeven_trigger_rr=1.0,
        breakeven_activated=False,
        volatility_sl_enabled=True,
        volatility_atr_period=14,
        volatility_atr_multiplier=2.5,
        volatility_last_atr=150.0,
        active_watchers=[
            WatcherConfig(
                indicator="RSI",
                params={"period": 14, "timeframe": "15m"},
                condition="> 75",
                action="tighten_sl",
                action_params={"sl_offset_atr": 1.5},
                is_active=True,
            )
        ],
        adjustment_priority=["watcher", "trailing", "breakeven", "volatility"],
        opened_at=now,
        closed_at=None,
        last_adjusted_at=now,
        realized_pnl=12.5,
        commission_total=0.8,
    )

    row = context.to_db_dict()

    # Emulate ORM row where JSON columns are returned as strings.
    json_string_row = dict(row)
    for key in (
        "sl_history_json",
        "tp_levels_json",
        "tp_history_json",
        "trailing_config_json",
        "breakeven_config_json",
        "volatility_config_json",
        "active_watchers_json",
        "adjustment_priority_json",
        "transition_log_json",
    ):
        json_string_row[key] = json.dumps(json_string_row[key])

    restored = PositionContext.from_db_row(json_string_row)
    assert restored.to_db_dict() == row


def test_sl_history_append_keeps_entry() -> None:
    context = PositionContext(position_id="pos-sl-history")
    entry = SLHistoryEntry(
        timestamp="2026-04-04T00:00:00+00:00",
        old_price=100.0,
        new_price=101.0,
        reason="manual",
        trigger_source="user",
        exchange_order_id="sl-123",
    )

    context.sl_history.append(entry)

    assert len(context.sl_history) == 1
    assert context.sl_history[0].exchange_order_id == "sl-123"
    assert context.sl_history[0].new_price == pytest.approx(101.0)


def test_tp_levels_trigger_price_computation_for_three_levels() -> None:
    entry_price = 100.0
    levels = [
        TPLevel.from_offset(
            level=0,
            price_offset_pct=1.0,
            close_pct=25.0,
            entry_price=entry_price,
            side=PositionSide.LONG,
        ),
        TPLevel.from_offset(
            level=1,
            price_offset_pct=2.0,
            close_pct=35.0,
            entry_price=entry_price,
            side=PositionSide.LONG,
        ),
        TPLevel.from_offset(
            level=2,
            price_offset_pct=3.0,
            close_pct=40.0,
            entry_price=entry_price,
            side=PositionSide.LONG,
        ),
    ]

    assert [level.level for level in levels] == [0, 1, 2]
    assert levels[0].trigger_price == pytest.approx(101.0)
    assert levels[1].trigger_price == pytest.approx(102.0)
    assert levels[2].trigger_price == pytest.approx(103.0)


def test_watcher_config_normalizes_indicator_and_condition() -> None:
    watcher = WatcherConfig(
        indicator="ema_cross",
        params={"fast": 21, "slow": 50},
        condition="cross_below",
        action="tighten_sl",
        action_params={},
        is_active=True,
    )

    assert watcher.indicator == "EMA_CROSS"
    assert watcher.condition == "cross_below"
    assert watcher.indicator in SUPPORTED_WATCHER_INDICATORS


def test_watcher_config_rejects_condition_with_indicator_name() -> None:
    with pytest.raises(ValueError, match="Invalid watcher condition format"):
        WatcherConfig(
            indicator="RSI",
            params={"period": 14},
            condition="RSI > 75",
            action="tighten_sl",
            action_params={},
            is_active=True,
        )


def test_watcher_config_rejects_unknown_indicator() -> None:
    with pytest.raises(ValueError, match="Unsupported watcher indicator"):
        WatcherConfig(
            indicator="MA_CROSSOVER",
            params={"fast": 21, "slow": 50},
            condition="cross_below",
            action="tighten_sl",
            action_params={},
            is_active=True,
        )
