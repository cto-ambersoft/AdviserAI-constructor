"""Pure tests for the Strategy anomaly detector (B6 — W12).

No DB: ``detect_anomalies`` is a pure, deterministic function over a strategy's
chronological trade series.
"""

from __future__ import annotations

from app.services.auto_trade.anomaly import AnomalyConfig, detect_anomalies
from app.services.auto_trade.anomaly.detector import (
    METRIC_PNL,
    METRIC_TRADE_FREQ,
    METRIC_WIN_RATE,
)


def test_pnl_outlier_is_flagged() -> None:
    # 19 small alternating trades + one large loss inside the rolling window.
    trade_pnls = ([1.0, -1.0] * 10)[:19] + [-50.0]
    findings = detect_anomalies(trade_pnls=trade_pnls)
    pnl = [f for f in findings if f.metric == METRIC_PNL]
    assert pnl, "expected a pnl_zscore finding for the outlier loss"
    assert pnl[0].z_score < 0  # an outlier *loss*
    assert pnl[0].severity in ("warning", "critical")


def test_flat_series_has_no_anomalies() -> None:
    # Perfectly steady strategy → zero variance everywhere → never flagged.
    assert detect_anomalies(trade_pnls=[1.0] * 30) == ()


def test_zero_pnl_series_has_no_anomalies() -> None:
    assert detect_anomalies(trade_pnls=[0.0] * 30) == ()


def test_short_series_below_window_is_no_anomaly() -> None:
    # Fewer trades than the rolling window → fail-safe, no findings.
    assert detect_anomalies(trade_pnls=[1.0, -1.0, 2.0, -2.0, 1.5]) == ()


def test_win_rate_collapse_is_flagged() -> None:
    # Long winning streak, then the final window collapses to all-losses.
    cfg = AnomalyConfig(z_threshold=2.0, window=5)
    trade_pnls = [1.0] * 24 + [-1.0] * 5
    findings = detect_anomalies(trade_pnls=trade_pnls, cfg=cfg)
    wr = [f for f in findings if f.metric == METRIC_WIN_RATE]
    assert wr, "expected a win_rate_collapse finding"
    assert wr[0].z_score < 0  # one-sided collapse


def test_trade_frequency_spike_is_flagged() -> None:
    # Steady cadence then a sudden burst in the last bucket.
    findings = detect_anomalies(
        trade_pnls=[1.0] * 10,
        bucket_counts=[5.0] * 9 + [50.0],
    )
    freq = [f for f in findings if f.metric == METRIC_TRADE_FREQ]
    assert freq, "expected a trade_frequency finding for the burst"
    assert freq[0].z_score > 0


def test_detection_is_deterministic() -> None:
    trade_pnls = ([1.0, -1.0] * 10)[:19] + [-50.0]
    assert detect_anomalies(trade_pnls=trade_pnls) == detect_anomalies(trade_pnls=trade_pnls)
