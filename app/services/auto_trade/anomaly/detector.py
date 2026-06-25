"""Strategy anomaly detector (B6 — W12).

Pure, deterministic statistical detection of anomalous strategy behaviour —
the decision half, no I/O (mirrors ``risk/kpi_guard.py``). The side effects
(the ``*/15`` sweep cron, emitting ``strategy_anomaly_detected``, dedup) live in
``app/worker/tasks.py`` / ``AutoTradeService``.

Four metrics on a strategy's chronological closed-trade series (decision #3):

1. ``pnl_zscore``        — rolling z-score of per-trade realized PnL; a large
   negative z is an outlier loss, large positive an outlier win.
2. ``drawdown_velocity`` — rolling z-score of the *increase* in running drawdown
   per trade; spikes catch a sudden equity slide.
3. ``win_rate_collapse`` — rolling win-rate dropping far below its own baseline
   (one-sided: only a *collapse* fires, z ≤ −threshold).
4. ``trade_frequency``   — last-bucket trade count vs an EWM baseline; catches
   both runaway bursts and sudden silence.

**Fail-safe by construction** (the cardinal rule, as in health/kpi_guard): an
insufficient sample (series shorter than the window) or a zero-variance window
yields a NaN z-score, which is treated as *no anomaly* — a fresh or perfectly
steady strategy is never flagged on noise. Detection is also **off by default**
at the config layer (``anomaly_detection_enabled=False``); this module is only
invoked once enabled.

Docs verified via context7: ``Series.rolling().mean()/.std()`` and
``Series.ewm().mean()`` (see tasks/m4-closeout-plan.md Appendix C).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

DEFAULT_Z_THRESHOLD = 3.0
DEFAULT_WINDOW = 20
# |z| at/above threshold*this is 'critical'; at/above threshold is 'warning'.
CRITICAL_Z_MULT = 1.5
DEFAULT_FREQ_ALPHA = 0.3

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

METRIC_PNL = "pnl_zscore"
METRIC_DD_VELOCITY = "drawdown_velocity"
METRIC_WIN_RATE = "win_rate_collapse"
METRIC_TRADE_FREQ = "trade_frequency"


@dataclass(frozen=True)
class AnomalyConfig:
    """Per-strategy detector thresholds (sourced from ``auto_trade_risk_configs``)."""

    z_threshold: float = DEFAULT_Z_THRESHOLD
    window: int = DEFAULT_WINDOW
    freq_alpha: float = DEFAULT_FREQ_ALPHA


@dataclass(frozen=True)
class AnomalyFinding:
    """One detected anomaly on one metric."""

    metric: str
    value: float
    baseline: float
    z_score: float
    severity: str


def _severity(z: float, threshold: float) -> str:
    az = abs(z)
    if az >= threshold * CRITICAL_Z_MULT:
        return SEVERITY_CRITICAL
    if az >= threshold:
        return SEVERITY_WARNING
    return SEVERITY_INFO


def _is_finite(x: float) -> bool:
    return not (math.isnan(x) or math.isinf(x))


# --- Helper series (also referenced from Appendix C) ---------------------------


def per_trade_pnl_zscore(trade_pnls: Sequence[float], window: int = DEFAULT_WINDOW) -> pd.Series:
    """z = (x − rolling_mean) / rolling_std for the *whole* series. NaN until the
    window fills.

    Convenience/illustrative helper (also used in Appendix C and tests) that
    returns the full z-series. The production path evaluates only the last point
    via :func:`_last_rolling_z`; this is kept separate so callers that want the
    whole curve (e.g. a chart) don't recompute it inline.
    """
    s = pd.Series(list(trade_pnls), dtype="float64")
    roll = s.rolling(window, min_periods=window)
    return (s - roll.mean()) / roll.std()


def trade_frequency_baseline(
    counts: Sequence[float], alpha: float = DEFAULT_FREQ_ALPHA
) -> pd.Series:
    """EWM baseline of per-bucket trade counts."""
    return pd.Series(list(counts), dtype="float64").ewm(alpha=alpha).mean()


# --- Internal z helpers --------------------------------------------------------


def _last_rolling_z(values: pd.Series, window: int) -> tuple[float, float]:
    """Return (z, baseline_mean) for the last point of a rolling z-score, or
    (nan, nan) when the sample is too short or the window has zero variance."""
    roll = values.rolling(window, min_periods=window)
    mean = roll.mean()
    std = roll.std()
    if len(values) == 0:
        return float("nan"), float("nan")
    last_mean = mean.iloc[-1]
    last_std = std.iloc[-1]
    last_val = values.iloc[-1]
    if not _is_finite(last_std) or last_std == 0.0 or not _is_finite(last_mean):
        return float("nan"), last_mean
    return (last_val - last_mean) / last_std, last_mean


# --- Metric detectors ----------------------------------------------------------


def _detect_pnl(trade_pnls: pd.Series, cfg: AnomalyConfig) -> AnomalyFinding | None:
    z, base = _last_rolling_z(trade_pnls, cfg.window)
    if not _is_finite(z) or abs(z) < cfg.z_threshold:
        return None
    return AnomalyFinding(
        metric=METRIC_PNL,
        value=float(trade_pnls.iloc[-1]),
        baseline=float(base),
        z_score=float(z),
        severity=_severity(z, cfg.z_threshold),
    )


def _detect_drawdown_velocity(trade_pnls: pd.Series, cfg: AnomalyConfig) -> AnomalyFinding | None:
    equity = trade_pnls.cumsum()
    running_max = equity.cummax()
    drawdown = running_max - equity  # >= 0, absolute USDT drawdown
    velocity = drawdown.diff()  # per-trade change in drawdown
    z, base = _last_rolling_z(velocity, cfg.window)
    # One-sided: only a *fast-rising* drawdown is anomalous.
    if not _is_finite(z) or z < cfg.z_threshold:
        return None
    return AnomalyFinding(
        metric=METRIC_DD_VELOCITY,
        value=float(velocity.iloc[-1]),
        baseline=float(base),
        z_score=float(z),
        severity=_severity(z, cfg.z_threshold),
    )


def _detect_win_rate_collapse(trade_pnls: pd.Series, cfg: AnomalyConfig) -> AnomalyFinding | None:
    """Flag the latest rolling win-rate collapsing below its own history.

    NOTE on baseline choice (vs the rolling-window z used by pnl/drawdown): a
    bounded ratio like win-rate has little local rolling variance, so its z is
    taken against the **whole-history** distribution of the rolling win-rate
    (mean/std over all valid points). This deliberately differs from the
    point-in-window z of the unbounded PnL/drawdown metrics; the global baseline
    drifts as history grows, which is acceptable for an alert-only detector and
    is the dimension to watch during calibration (P4-6).
    """
    wins = (trade_pnls > 0).astype("float64")
    rolling_wr = wins.rolling(cfg.window, min_periods=cfg.window).mean()
    valid = rolling_wr.dropna()
    if len(valid) < 2:
        return None
    mean = valid.mean()
    std = valid.std()
    if not _is_finite(std) or std == 0.0:
        return None
    z = (valid.iloc[-1] - mean) / std
    # One-sided: only a *collapse* (win-rate far below its baseline) fires.
    if not _is_finite(z) or z > -cfg.z_threshold:
        return None
    return AnomalyFinding(
        metric=METRIC_WIN_RATE,
        value=float(valid.iloc[-1] * 100.0),
        baseline=float(mean * 100.0),
        z_score=float(z),
        severity=_severity(z, cfg.z_threshold),
    )


def _detect_trade_frequency(
    bucket_counts: Sequence[float], cfg: AnomalyConfig
) -> AnomalyFinding | None:
    """Flag the latest bucket's trade count deviating from its EWM baseline.

    Like win-rate (and unlike pnl/drawdown), the deviation is z-scored against
    the **whole-history** residual std rather than a rolling window — the EWM
    baseline already tracks the recent level, so a global residual spread is the
    natural scale. Catches both bursts (z ≥ +threshold) and silence (z ≤ −threshold).
    """
    counts = pd.Series(list(bucket_counts), dtype="float64")
    if len(counts) < 2:
        return None
    baseline = trade_frequency_baseline(counts, cfg.freq_alpha)
    resid = counts - baseline
    sigma = resid.std()
    if not _is_finite(sigma) or sigma == 0.0:
        return None
    z = resid.iloc[-1] / sigma
    if not _is_finite(z) or abs(z) < cfg.z_threshold:
        return None
    return AnomalyFinding(
        metric=METRIC_TRADE_FREQ,
        value=float(counts.iloc[-1]),
        baseline=float(baseline.iloc[-1]),
        z_score=float(z),
        severity=_severity(z, cfg.z_threshold),
    )


def detect_anomalies(
    *,
    trade_pnls: Sequence[float],
    bucket_counts: Sequence[float] | None = None,
    cfg: AnomalyConfig | None = None,
) -> tuple[AnomalyFinding, ...]:
    """Evaluate all four metrics at the latest point of a strategy's trade series.

    ``trade_pnls``    — chronological per-trade realized PnL (USDT), oldest first.
                        Gross or net is fine — the detector measures *relative*
                        deviation, not an absolute PnL.
    ``bucket_counts`` — trade counts per fixed time bucket (e.g. per day),
                        chronological. Optional; frequency metric skipped if None.
    Returns only ``warning``/``critical`` findings (|z| ≥ threshold).
    """
    cfg = cfg or AnomalyConfig()
    series = pd.Series(list(trade_pnls), dtype="float64")
    findings: list[AnomalyFinding] = []
    for finding in (
        _detect_pnl(series, cfg),
        _detect_drawdown_velocity(series, cfg),
        _detect_win_rate_collapse(series, cfg),
        _detect_trade_frequency(bucket_counts, cfg) if bucket_counts is not None else None,
    ):
        # The detectors already gate on |z| ≥ threshold (≥ warning); the
        # severity check is belt-and-suspenders so an 'info' can never leak.
        if finding is not None and finding.severity != SEVERITY_INFO:
            findings.append(finding)
    return tuple(findings)
