"""Strategy Promotion Pipeline (B5 — W10): lifecycle FSM + KPI Gate."""

from app.services.auto_trade.promotion.kpi_gate import (
    GateCriterion,
    PromotionDecision,
    evaluate_promotion_gate,
)
from app.services.auto_trade.promotion.state_machine import (
    DEFAULT_NEW_STAGE,
    VALID_TRANSITIONS,
    InvalidPromotionError,
    LifecycleStage,
    PromotionStateMachine,
    PromotionTrigger,
    apply_transition,
    can_transition,
)

__all__ = [
    "DEFAULT_NEW_STAGE",
    "VALID_TRANSITIONS",
    "GateCriterion",
    "InvalidPromotionError",
    "LifecycleStage",
    "PromotionDecision",
    "PromotionStateMachine",
    "PromotionTrigger",
    "apply_transition",
    "can_transition",
    "evaluate_promotion_gate",
]
