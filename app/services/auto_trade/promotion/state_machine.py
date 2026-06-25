"""Strategy Promotion lifecycle FSM (B5 — W10).

A formal lifecycle for a strategy config: **Deep Research → Sandbox → KPI
Validation → Live**, with guard-checked transitions. Modelled as a hand-rolled
``VALID_TRANSITIONS`` dict + ``Enum``, exactly like the Position FSM
(``app/services/position/state_machine.py``) — no external state-machine lib.

This module is the **pure transition table**. The guard (KPI Gate) lives in
``promotion/kpi_gate.py``; the side effects (flipping ``lifecycle_stage``,
emitting events, requiring step-up) live in ``AutoTradeService``.

Stage semantics:
* ``research``   — strategy originates from the Deep-Research/forecast phase
  (the catalogue lives in the *core* service). Configs rarely persist here.
* ``sandbox``    — **verification stage** (decision Q2): the strategy runs for
  real on a *demo/testnet* account (no real money), accruing a genuine KPI track
  record. The order path / set_running guard (P4-4) blocks a non-live config from
  trading a real account. New configs default here and must pass the KPI gate to
  reach ``live``.
* ``validation`` — transient KPI-Gate evaluation between ``sandbox`` and
  ``live`` (contract: "KPI Validation").
* ``live``       — real-money execution.
* ``rejected`` / ``archived`` — terminal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class LifecycleStage(str, Enum):
    """Strategy promotion lifecycle stages."""

    RESEARCH = "research"
    SANDBOX = "sandbox"
    VALIDATION = "validation"
    LIVE = "live"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class PromotionTrigger(str, Enum):
    """Events that drive a lifecycle transition."""

    SUBMIT_TO_SANDBOX = "submit_to_sandbox"
    REQUEST_PROMOTION = "request_promotion"  # sandbox → validation (gate runs)
    GATE_PASSED = "gate_passed"  # validation → live (step-up gated)
    GATE_FAILED = "gate_failed"  # validation → sandbox
    DEMOTE = "demote"  # live → sandbox
    REJECT = "reject"
    ARCHIVE = "archive"


# Default stage a freshly created config enters (paper sandbox), as opposed to
# the column server-default ('live') used only to backfill pre-existing rows.
DEFAULT_NEW_STAGE = LifecycleStage.SANDBOX


VALID_TRANSITIONS: dict[LifecycleStage, dict[PromotionTrigger, LifecycleStage]] = {
    LifecycleStage.RESEARCH: {
        PromotionTrigger.SUBMIT_TO_SANDBOX: LifecycleStage.SANDBOX,
        PromotionTrigger.REJECT: LifecycleStage.REJECTED,
        PromotionTrigger.ARCHIVE: LifecycleStage.ARCHIVED,
    },
    LifecycleStage.SANDBOX: {
        PromotionTrigger.REQUEST_PROMOTION: LifecycleStage.VALIDATION,
        PromotionTrigger.REJECT: LifecycleStage.REJECTED,
        PromotionTrigger.ARCHIVE: LifecycleStage.ARCHIVED,
    },
    LifecycleStage.VALIDATION: {
        PromotionTrigger.GATE_PASSED: LifecycleStage.LIVE,
        PromotionTrigger.GATE_FAILED: LifecycleStage.SANDBOX,
        PromotionTrigger.ARCHIVE: LifecycleStage.ARCHIVED,
    },
    LifecycleStage.LIVE: {
        PromotionTrigger.DEMOTE: LifecycleStage.SANDBOX,
        PromotionTrigger.ARCHIVE: LifecycleStage.ARCHIVED,
    },
    # rejected / archived are terminal — no outgoing transitions.
    LifecycleStage.REJECTED: {},
    LifecycleStage.ARCHIVED: {},
}


class InvalidPromotionError(Exception):
    """Raised when a trigger is invalid for the current lifecycle stage."""


def can_transition(stage: LifecycleStage, trigger: PromotionTrigger) -> bool:
    """Return True when ``trigger`` is valid in ``stage``."""
    return trigger in VALID_TRANSITIONS.get(stage, {})


def apply_transition(stage: LifecycleStage, trigger: PromotionTrigger) -> LifecycleStage:
    """Return the next stage, or raise :class:`InvalidPromotionError`.

    Pure — no I/O, no logging. The caller persists the result and emits events.
    """
    transitions = VALID_TRANSITIONS.get(stage, {})
    if trigger not in transitions:
        raise InvalidPromotionError(
            f"Cannot {trigger.value} in stage {stage.value}"
        )
    return transitions[trigger]


class PromotionStateMachine:
    """Per-config promotion FSM with a transition log (mirrors PositionStateMachine)."""

    def __init__(
        self,
        config_id: int,
        initial_stage: LifecycleStage = DEFAULT_NEW_STAGE,
    ) -> None:
        self.config_id = config_id
        self.stage = initial_stage
        self._transition_log: list[dict] = []

    def can_transition(self, trigger: PromotionTrigger) -> bool:
        return can_transition(self.stage, trigger)

    def transition(
        self,
        trigger: PromotionTrigger,
        reason: str = "",
        metadata: Optional[dict] = None,
    ) -> LifecycleStage:
        old_stage = self.stage
        new_stage = apply_transition(old_stage, trigger)
        self.stage = new_stage
        self._transition_log.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "from": old_stage.value,
                "to": new_stage.value,
                "trigger": trigger.value,
                "reason": reason,
                "metadata": metadata or {},
            }
        )
        return new_stage

    def get_transition_log(self) -> list[dict]:
        return self._transition_log.copy()
