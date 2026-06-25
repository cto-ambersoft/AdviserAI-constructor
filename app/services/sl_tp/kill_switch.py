"""Volatility Kill-Switch spike detector (W9 — T2.2).

The **pure decision** half of the in-trade Volatility Kill-Switch (AC#4): given a
pre-computed ATR, its rolling baseline, and the last bar's % move, decide whether
a volatility spike warrants a hard auto-close. The I/O half — sourcing those
numbers from the realtime kline buffer (reusing ``RealtimeSLAdjuster.compute_atr``)
and issuing the market reduce-only close — lives in the live tracker / service
(T2.3).

Fail-safe by construction (the cardinal W9 rule): a close fires *only* on a
confirmed spike computed from data we actually have. Any missing input, a
degenerate (non-positive) baseline, or an unset threshold yields **no close** —
we never flatten a live position on absent or nonsensical data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KillSwitchSignal:
    """Outcome of the spike check. ``reason`` is the first-triggered branch."""

    should_close: bool
    reason: str | None = None  # "atr_spike" | "price_move"
    actual: float | None = None
    threshold: float | None = None


_NO_CLOSE = KillSwitchSignal(should_close=False)


def detect_volatility_spike(
    *,
    current_atr: float | None,
    baseline_atr: float | None,
    spike_mult: float | None,
    last_bar_move_pct: float | None,
    price_move_pct_threshold: float | None,
) -> KillSwitchSignal:
    """Decide whether a volatility spike warrants a hard auto-close.

    Two independent branches, first-trigger-wins:

    * **ATR spike** — ``current_atr >= spike_mult * baseline_atr``. Requires all
      three present and a strictly-positive baseline (a zero/negative baseline
      would make the test trivially true, so it is guarded → no close).
    * **Price move** — ``abs(last_bar_move_pct) >= price_move_pct_threshold``.

    An unset threshold (``None``) turns its branch off; a missing measurement
    (``None``) skips its branch. Neither tripping ⇒ :data:`_NO_CLOSE`.
    """
    if (
        spike_mult is not None
        and current_atr is not None
        and baseline_atr is not None
        and baseline_atr > 0
    ):
        trigger_level = spike_mult * baseline_atr
        if current_atr >= trigger_level:
            return KillSwitchSignal(
                should_close=True,
                reason="atr_spike",
                actual=current_atr,
                threshold=trigger_level,
            )

    if price_move_pct_threshold is not None and last_bar_move_pct is not None:
        move = abs(last_bar_move_pct)
        if move >= price_move_pct_threshold:
            return KillSwitchSignal(
                should_close=True,
                reason="price_move",
                actual=move,
                threshold=price_move_pct_threshold,
            )

    return _NO_CLOSE
