"""Unit tests for position state machine transitions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.position.state_machine import (  # noqa: E402
    VALID_TRANSITIONS,
    InvalidTransitionError,
    PositionState,
    PositionStateMachine,
    TransitionTrigger,
)


LEGAL_TRANSITION_CASES = [
    (state, trigger, expected)
    for state, transitions in VALID_TRANSITIONS.items()
    for trigger, expected in transitions.items()
]


@pytest.mark.parametrize(
    "initial_state, trigger, expected_state",
    LEGAL_TRANSITION_CASES,
    ids=[
        f"{state.value}->{trigger.value}->{expected.value}"
        for state, trigger, expected in LEGAL_TRANSITION_CASES
    ],
)
def test_all_legal_transitions_from_architecture_table(
    initial_state: PositionState,
    trigger: TransitionTrigger,
    expected_state: PositionState,
) -> None:
    machine = PositionStateMachine(position_id="pos-legal", initial_state=initial_state)

    assert machine.can_transition(trigger) is True
    assert machine.transition(trigger, reason="legal") == expected_state
    assert machine.state == expected_state


def test_happy_path_pending_entering_open() -> None:
    machine = PositionStateMachine(position_id="pos-entry", initial_state=PositionState.PENDING)

    assert machine.transition(TransitionTrigger.ENTRY_SUBMITTED) == PositionState.ENTERING
    assert machine.transition(TransitionTrigger.ENTRY_FILLED) == PositionState.OPEN
    assert machine.state == PositionState.OPEN


def test_open_adjusting_open_cycle() -> None:
    machine = PositionStateMachine(position_id="pos-adjust", initial_state=PositionState.OPEN)

    assert machine.transition(TransitionTrigger.INDICATOR_TRIGGER) == PositionState.ADJUSTING
    assert machine.transition(TransitionTrigger.ADJUSTMENT_COMPLETE) == PositionState.OPEN


def test_open_closing_closed_cycle() -> None:
    machine = PositionStateMachine(position_id="pos-close", initial_state=PositionState.OPEN)

    assert machine.transition(TransitionTrigger.MANUAL_CLOSE) == PositionState.CLOSING
    assert machine.transition(TransitionTrigger.ALL_CLOSED) == PositionState.CLOSED


@pytest.mark.parametrize(
    "initial_state, trigger",
    [
        (PositionState.PENDING, TransitionTrigger.ALL_CLOSED),
        (PositionState.CLOSED, TransitionTrigger.ENTRY_FILLED),
        (PositionState.CANCELLED, TransitionTrigger.ENTRY_SUBMITTED),
        (PositionState.FAILED, TransitionTrigger.ENTRY_SUBMITTED),
    ],
    ids=[
        "pending_to_closed_is_invalid",
        "closed_to_open_is_invalid",
        "cancelled_cannot_reenter",
        "failed_cannot_reenter",
    ],
)
def test_illegal_transitions_raise_invalid_transition_error(
    initial_state: PositionState,
    trigger: TransitionTrigger,
) -> None:
    machine = PositionStateMachine(position_id="pos-illegal", initial_state=initial_state)

    assert machine.can_transition(trigger) is False
    with pytest.raises(InvalidTransitionError):
        machine.transition(trigger)


def test_reconnecting_restores_pre_reconnect_state_on_sync_complete() -> None:
    machine = PositionStateMachine(position_id="pos-reconnect", initial_state=PositionState.OPEN)

    assert machine.transition(TransitionTrigger.WS_DISCONNECTED) == PositionState.RECONNECTING
    assert machine.transition(TransitionTrigger.SYNC_COMPLETE) == PositionState.OPEN
    assert machine.state == PositionState.OPEN


def test_error_recovery_to_open_via_emergency_sl() -> None:
    machine = PositionStateMachine(position_id="pos-recovery", initial_state=PositionState.ADJUSTING)

    assert machine.transition(TransitionTrigger.ADJUSTMENT_FAILED) == PositionState.ERROR_RECOVERY
    assert machine.transition(TransitionTrigger.EMERGENCY_SL_PLACED) == PositionState.OPEN
    assert machine.state == PositionState.OPEN


def test_transition_log_contains_expected_fields() -> None:
    machine = PositionStateMachine(position_id="pos-log", initial_state=PositionState.PENDING)
    machine.transition(TransitionTrigger.ENTRY_SUBMITTED, reason="submit", metadata={"step": 1})
    machine.transition(TransitionTrigger.ENTRY_FILLED, reason="filled", metadata={"step": 2})

    log = machine.get_transition_log()

    assert len(log) == 2
    for record in log:
        assert "timestamp" in record
        assert "from" in record
        assert "to" in record
        assert "trigger" in record
        assert "reason" in record
        datetime.fromisoformat(record["timestamp"])

    assert log[0]["from"] == PositionState.PENDING.value
    assert log[0]["to"] == PositionState.ENTERING.value
    assert log[0]["trigger"] == TransitionTrigger.ENTRY_SUBMITTED.value
    assert log[0]["reason"] == "submit"

    assert log[1]["from"] == PositionState.ENTERING.value
    assert log[1]["to"] == PositionState.OPEN.value
    assert log[1]["trigger"] == TransitionTrigger.ENTRY_FILLED.value
    assert log[1]["reason"] == "filled"


def test_partial_close_cycle_returns_to_open_when_position_still_open() -> None:
    machine = PositionStateMachine(position_id="pos-partial", initial_state=PositionState.OPEN)

    assert machine.transition(TransitionTrigger.PARTIAL_CLOSE) == PositionState.CLOSING
    assert machine.transition(TransitionTrigger.PARTIAL_CLOSE) == PositionState.OPEN
    assert machine.state == PositionState.OPEN


def test_adjusting_can_move_to_closing_on_sl_triggered() -> None:
    machine = PositionStateMachine(position_id="pos-adjust-sl", initial_state=PositionState.ADJUSTING)

    assert machine.transition(TransitionTrigger.SL_TRIGGERED) == PositionState.CLOSING
    assert machine.state == PositionState.CLOSING
