"""Position FSM."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class PositionState(str, Enum):
    """Position lifecycle states."""

    PENDING = "pending"
    ENTERING = "entering"
    OPEN = "open"
    ADJUSTING = "adjusting"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RECONNECTING = "reconnecting"
    ERROR_RECOVERY = "error_recovery"


class TransitionTrigger(str, Enum):
    """Events that can trigger state transitions."""

    # Entry
    ENTRY_SUBMITTED = "entry_submitted"
    ENTRY_FILLED = "entry_filled"
    ENTRY_REJECTED = "entry_rejected"
    ENTRY_TIMEOUT = "entry_timeout"
    ENTRY_CANCELLED = "entry_cancelled"

    # SL/TP adjustments
    INDICATOR_TRIGGER = "indicator_trigger"
    TRAILING_TICK = "trailing_tick"
    BREAKEVEN_REACHED = "breakeven_reached"
    VOLATILITY_SHIFT = "volatility_shift"
    ADJUSTMENT_COMPLETE = "adjustment_complete"
    ADJUSTMENT_FAILED = "adjustment_failed"

    # Closing
    SL_TRIGGERED = "sl_triggered"
    TP_TRIGGERED = "tp_triggered"
    PARTIAL_CLOSE = "partial_close"
    MANUAL_CLOSE = "manual_close"
    ALL_CLOSED = "all_closed"
    CLOSE_FAILED = "close_failed"

    # Fault tolerance
    WS_DISCONNECTED = "ws_disconnected"
    WS_RECONNECTED = "ws_reconnected"
    SYNC_COMPLETE = "sync_complete"
    EMERGENCY_SL_PLACED = "emergency_sl_placed"
    EMERGENCY_CLOSE = "emergency_close"


VALID_TRANSITIONS: dict[PositionState, dict[TransitionTrigger, PositionState]] = {
    PositionState.PENDING: {
        TransitionTrigger.ENTRY_SUBMITTED: PositionState.ENTERING,
        TransitionTrigger.ENTRY_CANCELLED: PositionState.CANCELLED,
    },
    PositionState.ENTERING: {
        TransitionTrigger.ENTRY_FILLED: PositionState.OPEN,
        TransitionTrigger.ENTRY_REJECTED: PositionState.FAILED,
        TransitionTrigger.ENTRY_TIMEOUT: PositionState.FAILED,
        TransitionTrigger.ENTRY_CANCELLED: PositionState.CANCELLED,
    },
    PositionState.OPEN: {
        TransitionTrigger.INDICATOR_TRIGGER: PositionState.ADJUSTING,
        TransitionTrigger.TRAILING_TICK: PositionState.ADJUSTING,
        TransitionTrigger.BREAKEVEN_REACHED: PositionState.ADJUSTING,
        TransitionTrigger.VOLATILITY_SHIFT: PositionState.ADJUSTING,
        TransitionTrigger.SL_TRIGGERED: PositionState.CLOSING,
        TransitionTrigger.TP_TRIGGERED: PositionState.CLOSING,
        TransitionTrigger.PARTIAL_CLOSE: PositionState.CLOSING,
        TransitionTrigger.MANUAL_CLOSE: PositionState.CLOSING,
        TransitionTrigger.WS_DISCONNECTED: PositionState.RECONNECTING,
    },
    PositionState.ADJUSTING: {
        TransitionTrigger.ADJUSTMENT_COMPLETE: PositionState.OPEN,
        TransitionTrigger.ADJUSTMENT_FAILED: PositionState.ERROR_RECOVERY,
        TransitionTrigger.SL_TRIGGERED: PositionState.CLOSING,
        TransitionTrigger.TP_TRIGGERED: PositionState.CLOSING,
        TransitionTrigger.MANUAL_CLOSE: PositionState.CLOSING,
        TransitionTrigger.WS_DISCONNECTED: PositionState.RECONNECTING,
    },
    PositionState.CLOSING: {
        TransitionTrigger.ALL_CLOSED: PositionState.CLOSED,
        TransitionTrigger.CLOSE_FAILED: PositionState.ERROR_RECOVERY,
        TransitionTrigger.PARTIAL_CLOSE: PositionState.OPEN,
        TransitionTrigger.WS_DISCONNECTED: PositionState.RECONNECTING,
    },
    PositionState.RECONNECTING: {
        TransitionTrigger.WS_RECONNECTED: PositionState.OPEN,
        TransitionTrigger.SYNC_COMPLETE: PositionState.OPEN,
        TransitionTrigger.ALL_CLOSED: PositionState.CLOSED,
    },
    PositionState.ERROR_RECOVERY: {
        TransitionTrigger.EMERGENCY_SL_PLACED: PositionState.OPEN,
        TransitionTrigger.EMERGENCY_CLOSE: PositionState.CLOSED,
    },
}


class InvalidTransitionError(Exception):
    """Raised when a trigger is invalid for the current state."""


class PositionStateMachine:
    """Per-position finite state machine with transition logging."""

    def __init__(
        self,
        position_id: str,
        initial_state: PositionState = PositionState.PENDING,
    ) -> None:
        self.position_id = position_id
        self.state = initial_state
        self._pre_reconnect_state: Optional[PositionState] = None
        self._transition_log: list[dict] = []

    def can_transition(self, trigger: TransitionTrigger) -> bool:
        """Return True when trigger is valid in current state."""
        transitions = VALID_TRANSITIONS.get(self.state, {})
        return trigger in transitions

    def transition(
        self,
        trigger: TransitionTrigger,
        reason: str = "",
        metadata: Optional[dict] = None,
    ) -> PositionState:
        """Execute transition or raise InvalidTransitionError."""
        transitions = VALID_TRANSITIONS.get(self.state, {})
        if trigger not in transitions:
            raise InvalidTransitionError(
                f"Cannot {trigger.value} in state {self.state.value} "
                f"for position {self.position_id}"
            )

        old_state = self.state
        new_state = transitions[trigger]

        if new_state == PositionState.RECONNECTING:
            self._pre_reconnect_state = old_state

        if trigger == TransitionTrigger.SYNC_COMPLETE and self._pre_reconnect_state:
            new_state = self._pre_reconnect_state
            self._pre_reconnect_state = None

        self.state = new_state
        self._transition_log.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "from": old_state.value,
                "to": new_state.value,
                "trigger": trigger.value,
                "reason": reason,
                "metadata": metadata or {},
            }
        )
        return new_state

    def get_transition_log(self) -> list[dict]:
        """Return copy of transition log."""
        return self._transition_log.copy()
