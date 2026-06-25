"""Pure FSM tests for the Strategy Promotion lifecycle (B5 — W10).

No DB: the transition table is a pure, deterministic mapping.
"""

from __future__ import annotations

import pytest

from app.services.auto_trade.promotion import (
    DEFAULT_NEW_STAGE,
    InvalidPromotionError,
    LifecycleStage,
    PromotionStateMachine,
    PromotionTrigger,
    apply_transition,
    can_transition,
)


def test_default_new_stage_is_sandbox() -> None:
    assert DEFAULT_NEW_STAGE is LifecycleStage.SANDBOX


def test_happy_path_sandbox_to_live() -> None:
    stage = LifecycleStage.SANDBOX
    stage = apply_transition(stage, PromotionTrigger.REQUEST_PROMOTION)
    assert stage is LifecycleStage.VALIDATION
    stage = apply_transition(stage, PromotionTrigger.GATE_PASSED)
    assert stage is LifecycleStage.LIVE


def test_gate_failed_returns_to_sandbox() -> None:
    stage = apply_transition(LifecycleStage.VALIDATION, PromotionTrigger.GATE_FAILED)
    assert stage is LifecycleStage.SANDBOX


def test_demote_live_to_sandbox() -> None:
    stage = apply_transition(LifecycleStage.LIVE, PromotionTrigger.DEMOTE)
    assert stage is LifecycleStage.SANDBOX


def test_research_can_enter_sandbox_or_be_rejected() -> None:
    assert apply_transition(LifecycleStage.RESEARCH, PromotionTrigger.SUBMIT_TO_SANDBOX) is (
        LifecycleStage.SANDBOX
    )
    assert apply_transition(LifecycleStage.RESEARCH, PromotionTrigger.REJECT) is (
        LifecycleStage.REJECTED
    )


@pytest.mark.parametrize(
    ("stage", "trigger"),
    [
        # Cannot skip straight from sandbox to live without validation.
        (LifecycleStage.SANDBOX, PromotionTrigger.GATE_PASSED),
        # Research cannot go directly live.
        (LifecycleStage.RESEARCH, PromotionTrigger.GATE_PASSED),
        # Terminal stages have no outgoing transitions.
        (LifecycleStage.REJECTED, PromotionTrigger.SUBMIT_TO_SANDBOX),
        (LifecycleStage.ARCHIVED, PromotionTrigger.DEMOTE),
        # Live cannot re-request promotion.
        (LifecycleStage.LIVE, PromotionTrigger.REQUEST_PROMOTION),
    ],
)
def test_invalid_transitions_raise(stage: LifecycleStage, trigger: PromotionTrigger) -> None:
    assert not can_transition(stage, trigger)
    with pytest.raises(InvalidPromotionError):
        apply_transition(stage, trigger)


def test_machine_logs_transitions() -> None:
    fsm = PromotionStateMachine(config_id=42)
    assert fsm.stage is LifecycleStage.SANDBOX
    fsm.transition(PromotionTrigger.REQUEST_PROMOTION, reason="trader requested")
    fsm.transition(PromotionTrigger.GATE_PASSED, reason="gate ok")
    assert fsm.stage is LifecycleStage.LIVE
    log = fsm.get_transition_log()
    assert [e["to"] for e in log] == ["validation", "live"]
    assert log[0]["reason"] == "trader requested"


def test_machine_invalid_transition_does_not_mutate_state() -> None:
    fsm = PromotionStateMachine(config_id=1, initial_stage=LifecycleStage.SANDBOX)
    with pytest.raises(InvalidPromotionError):
        fsm.transition(PromotionTrigger.GATE_PASSED)
    assert fsm.stage is LifecycleStage.SANDBOX
    assert fsm.get_transition_log() == []
