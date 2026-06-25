"""KPI-Guard evaluator (W9 — T1.2).

Decides whether a strategy's *live* KPIs have breached its configured KPI-Guard
and the strategy should be auto-paused (AC#4: "auto-pause on Max DD / Loss per
day"). This module is the **pure decision** half — deterministic, no I/O. The
side effect (locking the config, flipping ``is_running``, emitting events) lives
in ``AutoTradeService._auto_pause_strategy`` / ``apply_kpi_guard``.

Fail-safe by construction (the cardinal W9 rule): a pause fires *only* on a
configured threshold breached on data we actually have. The guard off, an absent
config, ``insufficient_data``, or a sample below the operator's
``kpi_guard_min_trades`` floor all yield no pause — a fresh or quiet strategy is
never halted on noise.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.auto_trade_risk_config import AutoTradeRiskConfig
from app.services.auto_trade.health import (
    HEALTH_CLASS_INSUFFICIENT,
    HEALTH_MIN_TRADES,
    StrategyHealth,
)


@dataclass(frozen=True)
class GuardBreach:
    """One breached KPI-Guard rule, with the actual value vs its threshold."""

    rule: str
    actual: float
    threshold: float


@dataclass(frozen=True)
class GuardDecision:
    """Outcome of evaluating the KPI-Guard for one strategy.

    ``warning`` is set (without a breach) when a rule had to be *skipped* on
    fail-safe grounds — currently the pct daily-loss rule when the account
    balance is unavailable. The caller surfaces it as a ``risk_check_degraded``
    event; it never causes a pause.
    """

    should_pause: bool
    breaches: tuple[GuardBreach, ...] = ()
    warning: str | None = None

    @property
    def rules(self) -> list[str]:
        return [breach.rule for breach in self.breaches]


_NO_PAUSE = GuardDecision(should_pause=False, breaches=())


def evaluate_kpi_guard(
    *,
    health: StrategyHealth,
    risk_cfg: AutoTradeRiskConfig | None,
    today_realized_pnl_usdt: float = 0.0,
    account_balance_usdt: float | None = None,
) -> GuardDecision:
    """Return the KPI-Guard decision for a strategy's current health reading.

    Two rule families, with different gating:

    * **Daily-loss** (``daily_loss`` / ``daily_loss_pct``) is a *hard same-day
      realized-loss* aggregate — it is **NOT** gated by sample size, because a
      runaway loss must halt a fresh strategy too (that is exactly when it
      matters). ``today_realized_pnl_usdt`` is signed (a loss is negative). The
      pct variant **fails open**: an unavailable/invalid balance skips the check
      with a ``warning``, never a pause (SPEC §6.3).
    * **Statistical** (``max_dd`` / ``min_win_rate``) only fire when the sample is
      reliable: at least ``HEALTH_MIN_TRADES`` (the hard floor below which
      ``compute_strategy_health`` zeroes the metrics) **and** at least the
      operator's ``kpi_guard_min_trades`` (an *additional*, higher floor — values
      below ``HEALTH_MIN_TRADES`` have no effect). ``insufficient_data`` is never
      judged. (Never judge a strategy on noise.)
    """
    if risk_cfg is None or not risk_cfg.kpi_guard_enabled:
        return _NO_PAUSE

    breaches: list[GuardBreach] = []
    warning: str | None = None

    # --- Daily-loss: hard same-day aggregate, ungated by sample size. ---
    realized_loss = -today_realized_pnl_usdt  # positive iff there is a loss today
    usdt_limit = risk_cfg.kpi_guard_max_daily_loss_usdt
    if usdt_limit is not None and realized_loss >= usdt_limit:
        breaches.append(GuardBreach(rule="daily_loss", actual=realized_loss, threshold=usdt_limit))
    pct_limit = risk_cfg.kpi_guard_max_daily_loss_pct
    if pct_limit is not None and realized_loss > 0:
        if account_balance_usdt is None or account_balance_usdt <= 0:
            warning = "daily_loss_pct skipped: account balance unavailable"
        else:
            loss_pct = realized_loss / account_balance_usdt * 100.0
            if loss_pct >= pct_limit:
                breaches.append(
                    GuardBreach(rule="daily_loss_pct", actual=loss_pct, threshold=pct_limit)
                )

    # --- Statistical rules: only on a reliable sample. ---
    # Hard floor: HEALTH_MIN_TRADES (below it compute_strategy_health zeroes the
    # metrics, so a lower kpi_guard_min_trades would be inert — make it explicit).
    reliable = (
        health.health_class != HEALTH_CLASS_INSUFFICIENT and health.sample_size >= HEALTH_MIN_TRADES
    )
    min_trades = risk_cfg.kpi_guard_min_trades
    if min_trades is not None and health.sample_size < min_trades:
        reliable = False
    if reliable:
        max_dd_limit = risk_cfg.kpi_guard_max_dd_pct
        if max_dd_limit is not None and health.max_dd_pct >= max_dd_limit:
            breaches.append(
                GuardBreach(rule="max_dd", actual=health.max_dd_pct, threshold=max_dd_limit)
            )
        win_rate_limit = risk_cfg.kpi_guard_min_win_rate_pct
        if win_rate_limit is not None and health.win_rate_pct < win_rate_limit:
            breaches.append(
                GuardBreach(
                    rule="min_win_rate",
                    actual=health.win_rate_pct,
                    threshold=win_rate_limit,
                )
            )

    return GuardDecision(should_pause=bool(breaches), breaches=tuple(breaches), warning=warning)
