"""Unit tests for MultiTPEngine."""

from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import ExchangeAdapter  # noqa: E402
from app.services.position.context import PositionContext, PositionSide, TPLevel  # noqa: E402
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
)
from app.services.position.state_machine import PositionState, TransitionTrigger  # noqa: E402
from app.services.sl_tp.multi_tp import MultiTPEngine  # noqa: E402


def _build_level(
    *,
    level_index: int,
    trigger_price: float,
    close_pct: float,
    move_sl_to: str | None,
    status: str = "pending",
) -> TPLevel:
    level = TPLevel(
        level=level_index,
        price_offset_pct=0.0,
        close_pct=close_pct,
        trigger_price=trigger_price,
        status=status,
        exchange_order_id=f"tp-{level_index + 1}",
    )
    level.__dict__["move_sl_to"] = move_sl_to
    return level


def _build_position(
    *,
    side: PositionSide = PositionSide.LONG,
    state: PositionState = PositionState.CLOSING,
    current_quantity: float = 1.0,
    tp1_status: str = "pending",
    tp2_status: str = "pending",
    tp3_status: str = "pending",
) -> PositionContext:
    return PositionContext(
        position_id="pos-mtp-1",
        symbol="BTC/USDT:USDT",
        side=side,
        state=state,
        entry_price=100000.0,
        original_quantity=1.0,
        current_quantity=current_quantity,
        current_sl_price=98000.0 if side == PositionSide.LONG else 102000.0,
        sl_exchange_order_id="sl-1",
        tp_levels=[
            _build_level(
                level_index=0,
                trigger_price=101000.0 if side == PositionSide.LONG else 99000.0,
                close_pct=33.0,
                move_sl_to="breakeven",
                status=tp1_status,
            ),
            _build_level(
                level_index=1,
                trigger_price=102000.0 if side == PositionSide.LONG else 98000.0,
                close_pct=33.0,
                move_sl_to="tp1",
                status=tp2_status,
            ),
            _build_level(
                level_index=2,
                trigger_price=103000.0 if side == PositionSide.LONG else 97000.0,
                close_pct=34.0,
                move_sl_to=None,
                status=tp3_status,
            ),
        ],
    )


async def test_initialize_tp_levels_enqueues_place_tp_for_all_pending_levels() -> None:
    position = _build_position(state=PositionState.OPEN)
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.initialize_tp_levels()

    assert order_queue.enqueue.await_count == 3
    tasks = [call.args[0] for call in order_queue.enqueue.await_args_list]
    assert all(isinstance(task, OrderTask) for task in tasks)
    assert all(task.action == "place_tp" for task in tasks)
    assert [task.params["quantity"] for task in tasks] == pytest.approx([0.33, 0.33, 0.34])


async def test_tp1_triggered_updates_position_history_and_partial_close_state() -> None:
    position = _build_position(current_quantity=1.0, state=PositionState.CLOSING)
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    assert position.current_quantity == pytest.approx(0.67)
    assert len(position.tp_history) == 1
    assert position.tp_history[0].tp_level == 0
    assert position.tp_levels[0].status == "triggered"
    assert order_queue.enqueue.await_count == 1
    assert position.state_machine.state == PositionState.OPEN
    assert position.state_machine.get_transition_log()[-1]["trigger"] == TransitionTrigger.PARTIAL_CLOSE


async def test_tp2_triggered_shifts_sl_to_tp1_and_reduces_qty_from_original() -> None:
    position = _build_position(
        current_quantity=0.67,
        state=PositionState.CLOSING,
        tp1_status="triggered",
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=1)

    assert position.current_quantity == pytest.approx(0.34)
    assert position.tp_levels[1].status == "triggered"
    task = order_queue.enqueue.await_args_list[0].args[0]
    assert task.action == "replace_sl"
    assert task.priority == OrderPriority.SL_ADJUSTMENT
    assert task.params["new_trigger_price"] == pytest.approx(position.tp_levels[0].trigger_price)


async def test_tp3_triggered_closes_position_and_transitions_all_closed() -> None:
    position = _build_position(
        current_quantity=0.34,
        state=PositionState.CLOSING,
        tp1_status="triggered",
        tp2_status="triggered",
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=2)

    assert position.current_quantity == pytest.approx(0.0)
    assert position.tp_levels[2].status == "triggered"
    assert order_queue.enqueue.await_count == 0
    assert position.state_machine.state == PositionState.CLOSED
    assert position.state_machine.get_transition_log()[-1]["trigger"] == TransitionTrigger.ALL_CLOSED


async def test_tp1_breakeven_move_enqueues_replace_sl_with_entry_price() -> None:
    position = _build_position(current_quantity=1.0, state=PositionState.CLOSING)
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    assert task.action == "replace_sl"
    assert task.params["trigger_price"] == pytest.approx(position.entry_price)
    assert task.params["new_trigger_price"] == pytest.approx(position.entry_price)


async def test_tp_trigger_with_remaining_open_levels_returns_to_open() -> None:
    position = _build_position(
        current_quantity=1.0,
        state=PositionState.OPEN,
        tp1_status="open",
        tp2_status="open",
        tp3_status="open",
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    assert position.state_machine.state == PositionState.OPEN
    assert position.tp_levels[0].status == "triggered"
    assert position.tp_levels[1].status == "open"
    assert position.tp_levels[2].status == "open"


# ─── sl_lock_pct (numeric) takes priority over move_sl_to ──────────────────


def _build_position_with_lock(
    *,
    side: PositionSide,
    entry: float,
    tp_offsets_and_locks: list[tuple[float, float | None]],
) -> PositionContext:
    """Build a position with explicit (price_offset_pct, sl_lock_pct) per level."""
    levels: list[TPLevel] = []
    for index, (offset, lock) in enumerate(tp_offsets_and_locks):
        trigger = (
            entry * (1.0 + offset / 100.0)
            if side == PositionSide.LONG
            else entry * (1.0 - offset / 100.0)
        )
        level = TPLevel(
            level=index,
            price_offset_pct=offset,
            close_pct=100.0 / len(tp_offsets_and_locks),
            trigger_price=trigger,
            status="open",
            exchange_order_id=f"tp-{index + 1}",
            sl_lock_pct=lock,
        )
        levels.append(level)
    return PositionContext(
        position_id="pos-lock-1",
        symbol="BTC/USDT:USDT",
        side=side,
        state=PositionState.OPEN,
        entry_price=entry,
        original_quantity=1.0,
        current_quantity=1.0,
        current_sl_price=entry * (0.99 if side == PositionSide.LONG else 1.01),
        sl_exchange_order_id="sl-1",
        tp_levels=levels,
    )


async def test_sl_lock_pct_zero_long_resolves_to_entry() -> None:
    position = _build_position_with_lock(
        side=PositionSide.LONG,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, 0.0), (3.0, None)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    order_queue.enqueue.assert_awaited()
    task = order_queue.enqueue.await_args_list[0].args[0]
    assert task.action == "replace_sl"
    # 0% lock → SL at entry (breakeven semantics).
    assert task.params["new_trigger_price"] == pytest.approx(100_000.0)


async def test_sl_lock_pct_fifty_long_resolves_halfway() -> None:
    position = _build_position_with_lock(
        side=PositionSide.LONG,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, 50.0), (3.0, None)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    # TP1 at 101_000, 50% lock → halfway: 100_500.
    assert task.params["new_trigger_price"] == pytest.approx(100_500.0)


async def test_sl_lock_pct_hundred_long_resolves_to_tp_price() -> None:
    position = _build_position_with_lock(
        side=PositionSide.LONG,
        entry=100_000.0,
        tp_offsets_and_locks=[(2.0, 100.0)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    # 100% lock → SL right at TP price.
    assert task.params["new_trigger_price"] == pytest.approx(102_000.0)


async def test_sl_lock_pct_short_zero_resolves_to_entry() -> None:
    position = _build_position_with_lock(
        side=PositionSide.SHORT,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, 0.0)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    # SHORT entry=100k, TP1 at 99k → 0% lock → SL = entry = 100k.
    assert task.params["new_trigger_price"] == pytest.approx(100_000.0)


async def test_sl_lock_pct_short_fifty_resolves_halfway() -> None:
    position = _build_position_with_lock(
        side=PositionSide.SHORT,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, 50.0)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    # SHORT entry=100k, TP1 at 99k → halfway = 99_500.
    assert task.params["new_trigger_price"] == pytest.approx(99_500.0)


async def test_sl_lock_pct_takes_priority_over_move_sl_to() -> None:
    """When both sl_lock_pct and move_sl_to are set, lock_pct wins."""
    position = _build_position_with_lock(
        side=PositionSide.LONG,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, 50.0)],
    )
    # Inject conflicting legacy string — should be ignored.
    position.tp_levels[0].move_sl_to = "breakeven"

    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    # 50% lock wins → 100_500, not breakeven=100_000.
    assert task.params["new_trigger_price"] == pytest.approx(100_500.0)


async def test_legacy_move_sl_to_breakeven_still_works_when_lock_pct_unset() -> None:
    position = _build_position_with_lock(
        side=PositionSide.LONG,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, None)],
    )
    position.tp_levels[0].move_sl_to = "breakeven"

    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    assert task.params["new_trigger_price"] == pytest.approx(100_000.0)


async def test_no_lock_pct_and_no_move_sl_to_does_not_enqueue_replace_sl() -> None:
    position = _build_position_with_lock(
        side=PositionSide.LONG,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, None), (3.0, None)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    # Neither lock_pct nor move_sl_to → SL not touched.
    actions = [
        call.args[0].action for call in order_queue.enqueue.await_args_list
    ]
    assert "replace_sl" not in actions


async def test_sl_lock_pct_negative_long_places_sl_below_entry() -> None:
    """Negative lock_pct = move SL BELOW entry, between entry and original SL.

    Use case: after TP1 fires, user wants to reduce risk but not all the way to
    breakeven. e.g. -50 → halfway between entry and what would be a mirror of TP.
    """
    position = _build_position_with_lock(
        side=PositionSide.LONG,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, -50.0)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    # entry=100k, TP1=101k → -50% → 100k + (1k × -0.5) = 99,500.
    assert task.params["new_trigger_price"] == pytest.approx(99_500.0)


async def test_sl_lock_pct_negative_short_places_sl_above_entry() -> None:
    """Mirror case for SHORT: negative lock_pct moves SL ABOVE entry."""
    position = _build_position_with_lock(
        side=PositionSide.SHORT,
        entry=100_000.0,
        tp_offsets_and_locks=[(1.0, -50.0)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    await engine.handle_tp_triggered(triggered_level=0)

    task = order_queue.enqueue.await_args_list[0].args[0]
    # SHORT entry=100k, TP1=99k → -50% → 100k + (-1k × -0.5) = 100,500.
    assert task.params["new_trigger_price"] == pytest.approx(100_500.0)


async def test_sl_lock_pct_resolver_handles_full_negative_range() -> None:
    """Direct verification of the resolver math: SL = entry + (TP-entry) × pct/100."""
    position = _build_position_with_lock(
        side=PositionSide.LONG,
        entry=100_000.0,
        tp_offsets_and_locks=[(2.0, None)],
    )
    adapter = AsyncMock(spec=ExchangeAdapter)
    order_queue = AsyncMock(spec=OrderExecutionQueue)
    engine = MultiTPEngine(position, adapter, order_queue)

    cases = [(-100.0, 98_000.0), (-25.0, 99_500.0), (75.0, 101_500.0)]
    for lock_pct, expected_sl in cases:
        position.tp_levels[0].sl_lock_pct = lock_pct
        result = engine._resolve_sl_shift_target(position.tp_levels[0])
        assert result is not None
        assert result == pytest.approx(expected_sl), (
            f"lock_pct={lock_pct} gave {result}, expected {expected_sl}"
        )
