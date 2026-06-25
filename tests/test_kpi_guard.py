"""Pure-engine tests for the W9 KPI-Guard evaluator (T1.2).

No DB: ``evaluate_kpi_guard`` is a pure, deterministic function over a
``StrategyHealth`` reading and a (transient) risk-config row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.auto_trade_risk_config import AutoTradeRiskConfig
from app.services.auto_trade.health import StrategyHealth
from app.services.auto_trade.risk import evaluate_kpi_guard


def _health(
    *, max_dd_pct: float, sample_size: int = 12, health_class: str = "warning"
) -> StrategyHealth:
    return StrategyHealth(
        config_id=1,
        window_days=30,
        sample_size=sample_size,
        win_rate_pct=50.0,
        max_dd_pct=max_dd_pct,
        total_pnl_usdt=-10.0,
        roi_pct=-10.0,
        sharpe_proxy=0.0,
        stability_score=0.5,
        health_score=45.0,
        health_class=health_class,
        computed_at=datetime(2026, 6, 5, tzinfo=UTC),
    )


def _cfg(
    *,
    kpi_guard_enabled: bool = True,
    kpi_guard_max_dd_pct: float | None = None,
    kpi_guard_max_daily_loss_usdt: float | None = None,
    kpi_guard_max_daily_loss_pct: float | None = None,
    kpi_guard_min_win_rate_pct: float | None = None,
    kpi_guard_min_trades: int | None = None,
) -> AutoTradeRiskConfig:
    return AutoTradeRiskConfig(
        config_id=1,
        enabled=True,
        kpi_guard_enabled=kpi_guard_enabled,
        kpi_guard_max_dd_pct=kpi_guard_max_dd_pct,
        kpi_guard_max_daily_loss_usdt=kpi_guard_max_daily_loss_usdt,
        kpi_guard_max_daily_loss_pct=kpi_guard_max_daily_loss_pct,
        kpi_guard_min_win_rate_pct=kpi_guard_min_win_rate_pct,
        kpi_guard_min_trades=kpi_guard_min_trades,
    )


def test_kpi_guard_max_dd_breach_pauses() -> None:
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=35.0),
        risk_cfg=_cfg(kpi_guard_max_dd_pct=20.0, kpi_guard_min_trades=10),
    )
    assert decision.should_pause is True
    assert [b.rule for b in decision.breaches] == ["max_dd"]
    breach = decision.breaches[0]
    assert breach.actual == 35.0
    assert breach.threshold == 20.0


def test_kpi_guard_within_limit_does_not_pause() -> None:
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=15.0),
        risk_cfg=_cfg(kpi_guard_max_dd_pct=20.0),
    )
    assert decision.should_pause is False
    assert decision.breaches == ()


def test_kpi_guard_threshold_none_does_not_pause() -> None:
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=99.0),
        risk_cfg=_cfg(kpi_guard_max_dd_pct=None),
    )
    assert decision.should_pause is False


def test_kpi_guard_disabled_never_pauses() -> None:
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=99.0),
        risk_cfg=_cfg(kpi_guard_enabled=False, kpi_guard_max_dd_pct=1.0),
    )
    assert decision.should_pause is False


def test_kpi_guard_none_config_never_pauses() -> None:
    decision = evaluate_kpi_guard(health=_health(max_dd_pct=99.0), risk_cfg=None)
    assert decision.should_pause is False


def test_kpi_guard_insufficient_data_never_pauses() -> None:
    # Fail-safe: a huge drawdown does NOT pause a statistically unreliable strategy.
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=99.0, sample_size=3, health_class="insufficient_data"),
        risk_cfg=_cfg(kpi_guard_max_dd_pct=10.0, kpi_guard_min_trades=2),
    )
    assert decision.should_pause is False


def test_kpi_guard_below_min_trades_floor_never_pauses() -> None:
    # Operator's own floor: too few live trades to act on, even if breaching.
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=50.0, sample_size=5, health_class="warning"),
        risk_cfg=_cfg(kpi_guard_max_dd_pct=10.0, kpi_guard_min_trades=10),
    )
    assert decision.should_pause is False


def test_kpi_guard_statistical_rules_respect_health_min_trades_floor() -> None:
    # Review I2 — the statistical rules (max_dd / min_win_rate) must not fire below
    # HEALTH_MIN_TRADES (10) even when the operator's kpi_guard_min_trades is lower
    # and health_class is not 'insufficient_data'. The hard 10-trade floor (where
    # compute_strategy_health zeroes the metrics) is enforced explicitly here.
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=50.0, sample_size=7, health_class="warning"),
        risk_cfg=_cfg(kpi_guard_max_dd_pct=10.0, kpi_guard_min_trades=5),
    )
    assert decision.should_pause is False


def test_kpi_guard_daily_loss_usdt_breach_pauses_even_when_insufficient() -> None:
    # T1.4 — daily-loss is a HARD same-day aggregate: fires regardless of sample
    # size (a runaway loss must halt a fresh strategy too — it matters most there).
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=0.0, sample_size=3, health_class="insufficient_data"),
        risk_cfg=_cfg(kpi_guard_max_daily_loss_usdt=30.0),
        today_realized_pnl_usdt=-60.0,
    )
    assert decision.should_pause is True
    assert [b.rule for b in decision.breaches] == ["daily_loss"]
    assert decision.breaches[0].actual == 60.0
    assert decision.breaches[0].threshold == 30.0


def test_kpi_guard_daily_loss_usdt_within_limit() -> None:
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=0.0),
        risk_cfg=_cfg(kpi_guard_max_daily_loss_usdt=30.0),
        today_realized_pnl_usdt=-20.0,
    )
    assert decision.should_pause is False


def test_kpi_guard_daily_loss_usdt_no_loss_does_not_pause() -> None:
    # A profitable day never trips the loss limit.
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=0.0),
        risk_cfg=_cfg(kpi_guard_max_daily_loss_usdt=30.0),
        today_realized_pnl_usdt=120.0,
    )
    assert decision.should_pause is False


def test_kpi_guard_daily_loss_pct_breach() -> None:
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=0.0),
        risk_cfg=_cfg(kpi_guard_max_daily_loss_pct=50.0),
        today_realized_pnl_usdt=-60.0,
        account_balance_usdt=100.0,
    )
    assert decision.should_pause is True
    assert [b.rule for b in decision.breaches] == ["daily_loss_pct"]
    assert decision.breaches[0].actual == 60.0
    assert decision.breaches[0].threshold == 50.0


def test_kpi_guard_daily_loss_pct_fails_open_on_missing_balance() -> None:
    # Fail-open (SPEC §6.3): an unavailable balance ⇒ degraded warning, NEVER pause.
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=0.0),
        risk_cfg=_cfg(kpi_guard_max_daily_loss_pct=50.0),
        today_realized_pnl_usdt=-60.0,
        account_balance_usdt=None,
    )
    assert decision.should_pause is False
    assert decision.warning is not None


def test_kpi_guard_min_win_rate_breach_when_reliable() -> None:
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=0.0, sample_size=12),  # _health default win_rate_pct=50
        risk_cfg=_cfg(kpi_guard_min_win_rate_pct=60.0, kpi_guard_min_trades=10),
    )
    assert decision.should_pause is True
    assert [b.rule for b in decision.breaches] == ["min_win_rate"]
    assert decision.breaches[0].actual == 50.0
    assert decision.breaches[0].threshold == 60.0


def test_kpi_guard_min_win_rate_silent_below_min_trades() -> None:
    decision = evaluate_kpi_guard(
        health=_health(max_dd_pct=0.0, sample_size=5),
        risk_cfg=_cfg(kpi_guard_min_win_rate_pct=60.0, kpi_guard_min_trades=10),
    )
    assert decision.should_pause is False


def test_kpi_guard_sweep_cron_registered() -> None:
    # T1.3 — the guard sweep runs every 5 minutes (UTC) via LabelScheduleSource.
    from app.worker import tasks as worker_tasks

    schedule = worker_tasks.evaluate_kpi_guards.labels["schedule"]
    assert any(entry.get("cron") == "*/5 * * * *" for entry in schedule)
    assert any(entry.get("schedule_id") == "kpi_guard_every_5m" for entry in schedule)
