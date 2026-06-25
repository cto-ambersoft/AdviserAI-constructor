"""Pure-engine tests for the W9 Volatility Kill-Switch spike detector (T2.2).

No I/O: ``detect_volatility_spike`` is pure arithmetic over a pre-computed ATR /
baseline / last-bar move. Fail-safe by construction — missing or insufficient
data must NEVER trip (we don't close a real position on absent data).
"""

from __future__ import annotations

from app.services.sl_tp.kill_switch import KillSwitchSignal, detect_volatility_spike


def _detect(
    *,
    current_atr: float | None = None,
    baseline_atr: float | None = None,
    spike_mult: float | None = None,
    last_bar_move_pct: float | None = None,
    price_move_pct_threshold: float | None = None,
) -> KillSwitchSignal:
    return detect_volatility_spike(
        current_atr=current_atr,
        baseline_atr=baseline_atr,
        spike_mult=spike_mult,
        last_bar_move_pct=last_bar_move_pct,
        price_move_pct_threshold=price_move_pct_threshold,
    )


def test_atr_spike_trips() -> None:
    sig = _detect(current_atr=250.0, baseline_atr=100.0, spike_mult=2.0)
    assert sig.should_close is True
    assert sig.reason == "atr_spike"
    assert sig.actual == 250.0
    assert sig.threshold == 200.0  # spike_mult * baseline


def test_atr_spike_within_band_does_not_trip() -> None:
    sig = _detect(current_atr=150.0, baseline_atr=100.0, spike_mult=2.0)
    assert sig.should_close is False
    assert sig.reason is None


def test_price_move_trips_on_abs_move() -> None:
    sig = _detect(last_bar_move_pct=-6.0, price_move_pct_threshold=5.0)
    assert sig.should_close is True
    assert sig.reason == "price_move"
    assert sig.actual == 6.0  # absolute move
    assert sig.threshold == 5.0


def test_price_move_within_threshold_does_not_trip() -> None:
    sig = _detect(last_bar_move_pct=3.0, price_move_pct_threshold=5.0)
    assert sig.should_close is False


def test_missing_current_atr_never_trips() -> None:
    sig = _detect(current_atr=None, baseline_atr=100.0, spike_mult=2.0)
    assert sig.should_close is False


def test_missing_baseline_never_trips() -> None:
    sig = _detect(current_atr=250.0, baseline_atr=None, spike_mult=2.0)
    assert sig.should_close is False


def test_nonpositive_baseline_never_trips() -> None:
    # A zero/negative baseline would make `current >= mult*baseline` trivially true —
    # must be guarded so we never close on a degenerate baseline.
    sig = _detect(current_atr=50.0, baseline_atr=0.0, spike_mult=2.0)
    assert sig.should_close is False


def test_thresholds_none_means_off() -> None:
    # Both branches off ⇒ never trips, even on a wild ATR / move.
    sig = _detect(current_atr=9999.0, baseline_atr=1.0, last_bar_move_pct=99.0)
    assert sig.should_close is False


def test_atr_spike_takes_precedence_when_both_trip() -> None:
    sig = _detect(
        current_atr=250.0,
        baseline_atr=100.0,
        spike_mult=2.0,
        last_bar_move_pct=-9.0,
        price_move_pct_threshold=5.0,
    )
    assert sig.should_close is True
    assert sig.reason == "atr_spike"  # first-trigger-wins
