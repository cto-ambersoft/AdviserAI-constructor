"""Pure tests for the Strategy Promotion KPI Gate (B5 — W10).

No DB: ``evaluate_promotion_gate`` is a pure, deterministic function over a
``StrategyHealth`` reading, the (transient) risk-config row, and the days the
strategy has spent in sandbox. It is the inverse of the KPI-Guard: promote when
*all* criteria pass, fail-safe to "cannot promote" on an unreliable sample.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.auto_trade_risk_config import AutoTradeRiskConfig
from app.services.auto_trade.health import HEALTH_CLASS_INSUFFICIENT, StrategyHealth
from app.services.auto_trade.promotion import evaluate_promotion_gate


def _health(
    *,
    win_rate_pct: float = 60.0,
    max_dd_pct: float = 10.0,
    sample_size: int = 30,
    health_class: str = "healthy",
) -> StrategyHealth:
    return StrategyHealth(
        config_id=1,
        window_days=30,
        sample_size=sample_size,
        win_rate_pct=win_rate_pct,
        max_dd_pct=max_dd_pct,
        total_pnl_usdt=100.0,
        roi_pct=10.0,
        sharpe_proxy=1.0,
        stability_score=0.7,
        health_score=75.0,
        health_class=health_class,
        computed_at=datetime(2026, 6, 18, tzinfo=UTC),
    )


def _cfg(**overrides: object) -> AutoTradeRiskConfig:
    return AutoTradeRiskConfig(config_id=1, **overrides)


def test_healthy_strategy_passes_all_criteria() -> None:
    decision = evaluate_promotion_gate(health=_health(), risk_cfg=None, sandbox_days=10.0)
    assert decision.can_promote is True
    assert decision.failed == ()


def test_low_win_rate_blocks_promotion() -> None:
    decision = evaluate_promotion_gate(
        health=_health(win_rate_pct=40.0), risk_cfg=None, sandbox_days=10.0
    )
    assert decision.can_promote is False
    assert "min_win_rate" in [c.name for c in decision.failed]


def test_high_max_dd_blocks_promotion() -> None:
    decision = evaluate_promotion_gate(
        health=_health(max_dd_pct=30.0), risk_cfg=None, sandbox_days=10.0
    )
    assert decision.can_promote is False
    assert "max_dd" in [c.name for c in decision.failed]


def test_insufficient_sandbox_days_blocks_promotion() -> None:
    decision = evaluate_promotion_gate(health=_health(), risk_cfg=None, sandbox_days=3.0)
    assert decision.can_promote is False
    assert "min_sandbox_days" in [c.name for c in decision.failed]


def test_insufficient_sample_blocks_promotion() -> None:
    # Never promote on noise — a fresh strategy cannot pass the gate.
    decision = evaluate_promotion_gate(
        health=_health(sample_size=5, health_class=HEALTH_CLASS_INSUFFICIENT),
        risk_cfg=None,
        sandbox_days=30.0,
    )
    assert decision.can_promote is False
    assert "min_trades" in [c.name for c in decision.failed]


def test_custom_thresholds_override_defaults() -> None:
    # A 60% win rate passes the 50% default but fails a stricter 70% bar.
    decision = evaluate_promotion_gate(
        health=_health(win_rate_pct=60.0),
        risk_cfg=_cfg(promote_min_win_rate_pct=70.0),
        sandbox_days=10.0,
    )
    assert decision.can_promote is False
    assert "min_win_rate" in [c.name for c in decision.failed]


def test_criteria_are_reported_with_actual_and_threshold() -> None:
    decision = evaluate_promotion_gate(health=_health(), risk_cfg=None, sandbox_days=10.0)
    names = {c.name for c in decision.criteria}
    assert names == {"min_trades", "min_sandbox_days", "min_win_rate", "max_dd"}
    wr = next(c for c in decision.criteria if c.name == "min_win_rate")
    assert wr.actual == 60.0 and wr.threshold == 50.0 and wr.passed is True
