"""Promotion KPI Gate (B5 — W10).

The guard on the sandbox→live transition: a strategy may be promoted only when
its *live* (paper, in sandbox) KPIs clear every configured bar **and** it has
served a minimum period in sandbox. This is the **pure decision** half — no I/O
— and is the mirror image of ``risk/kpi_guard.py``: the guard *pauses* a live
strategy when a KPI is breached; the gate *promotes* a sandbox strategy when
every KPI passes.

Fail-safe by construction (same cardinal rule as health/kpi_guard): a strategy
with an unreliable sample (``insufficient_data`` or fewer than the configured
``promote_min_trades``) **cannot** be promoted — a fresh strategy is never
waved through on noise. The statistical criteria (win-rate, max-DD) are reported
as failed in that case rather than passing on zeroed metrics.

Unlike kpi_guard (where a ``NULL`` threshold means "rule off"), a ``NULL`` here
means "use the gate's conservative built-in default": a promotion gate always
has criteria — you cannot promote against no bar.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.auto_trade_risk_config import AutoTradeRiskConfig
from app.services.auto_trade.health import (
    HEALTH_CLASS_INSUFFICIENT,
    HEALTH_MIN_TRADES,
    StrategyHealth,
)

# Conservative built-in defaults, used when a threshold is NULL. Calibrate with
# traders before arming on real money (real-money safety).
DEFAULT_PROMOTE_MIN_WIN_RATE_PCT = 50.0
DEFAULT_PROMOTE_MAX_DD_PCT = 25.0
DEFAULT_PROMOTE_MIN_TRADES = 20
DEFAULT_PROMOTE_MIN_SANDBOX_DAYS = 7.0


@dataclass(frozen=True)
class GateCriterion:
    """One promotion criterion, with the actual value vs its threshold."""

    name: str
    actual: float
    threshold: float
    passed: bool


@dataclass(frozen=True)
class PromotionDecision:
    """Outcome of evaluating the promotion gate for one strategy."""

    can_promote: bool
    criteria: tuple[GateCriterion, ...] = ()

    @property
    def passed(self) -> tuple[GateCriterion, ...]:
        return tuple(c for c in self.criteria if c.passed)

    @property
    def failed(self) -> tuple[GateCriterion, ...]:
        return tuple(c for c in self.criteria if not c.passed)


def _resolve(value: float | int | None, default: float | int) -> float:
    return float(default if value is None else value)


def evaluate_promotion_gate(
    *,
    health: StrategyHealth,
    risk_cfg: AutoTradeRiskConfig | None,
    sandbox_days: float,
) -> PromotionDecision:
    """Return the promotion-gate decision for a strategy's current health.

    ``sandbox_days`` is the time the strategy has spent in sandbox (the caller
    derives it from the lifecycle history / last sandbox entry). All four
    criteria must pass for ``can_promote`` to be True.
    """
    min_trades = int(
        _resolve(
            risk_cfg.promote_min_trades if risk_cfg else None,
            DEFAULT_PROMOTE_MIN_TRADES,
        )
    )
    min_sandbox_days = _resolve(
        risk_cfg.promote_min_sandbox_days if risk_cfg else None,
        DEFAULT_PROMOTE_MIN_SANDBOX_DAYS,
    )
    min_win_rate = _resolve(
        risk_cfg.promote_min_win_rate_pct if risk_cfg else None,
        DEFAULT_PROMOTE_MIN_WIN_RATE_PCT,
    )
    max_dd = _resolve(
        risk_cfg.promote_max_dd_pct if risk_cfg else None,
        DEFAULT_PROMOTE_MAX_DD_PCT,
    )

    # Sample must clear both the hard health floor and the operator's min_trades.
    reliable = (
        health.health_class != HEALTH_CLASS_INSUFFICIENT
        and health.sample_size >= HEALTH_MIN_TRADES
        and health.sample_size >= min_trades
    )

    criteria = (
        GateCriterion(
            name="min_trades",
            actual=float(health.sample_size),
            threshold=float(min_trades),
            passed=reliable,
        ),
        GateCriterion(
            name="min_sandbox_days",
            actual=sandbox_days,
            threshold=min_sandbox_days,
            passed=sandbox_days >= min_sandbox_days,
        ),
        GateCriterion(
            name="min_win_rate",
            actual=health.win_rate_pct,
            # Statistical criteria only count on a reliable sample; on an
            # unreliable one they are reported failed (never pass on noise).
            threshold=min_win_rate,
            passed=reliable and health.win_rate_pct >= min_win_rate,
        ),
        GateCriterion(
            name="max_dd",
            actual=health.max_dd_pct,
            threshold=max_dd,
            passed=reliable and health.max_dd_pct <= max_dd,
        ),
    )

    return PromotionDecision(
        can_promote=all(c.passed for c in criteria),
        criteria=criteria,
    )
