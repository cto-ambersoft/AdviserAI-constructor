"""Pure, deterministic overlay computations.

Every function in this module is side-effect-free: same inputs always
produce the same outputs. The auto-trade service composes these primitives
with the resolver (DB I/O) and the audit writer (DB I/O) — keeping the
math testable in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.schemas.ai_overlay import AiOverlayConfig, AiTrendSnapshot


PositionSide = Literal["long", "short"]


@dataclass(frozen=True)
class OverlayDecision:
    """Result of a single scaler invocation.

    ``changed`` is False when the overlay would not move the parameter
    (flat trend, weak strength, or overlay-disabled). Callers can use it
    to skip emitting an audit row.
    """

    changed: bool
    reason: str


def _effective_strength(snapshot: AiTrendSnapshot, overlay: AiOverlayConfig) -> float:
    """Strength to use in formulas. Zero below ``min_strength`` or for ``flat``."""
    if snapshot.direction == "flat":
        return 0.0
    if snapshot.strength < overlay.min_strength:
        return 0.0
    return snapshot.strength


def should_block_entry(
    intended_side: PositionSide,
    snapshot: AiTrendSnapshot,
    overlay: AiOverlayConfig,
) -> tuple[bool, str]:
    """Phase 1 — block entries that contradict a confident ai_trend.

    Mirrors the ``ai_entry_side_lock`` semantics used in backtest:
    - ``direction=up``  + strength >= min_strength  → block ``short``.
    - ``direction=down`` + strength >= min_strength → block ``long``.
    - ``direction=flat`` or weak strength           → never block.
    """
    if not overlay.enabled or not overlay.entry_side_lock_enabled:
        return False, "overlay_disabled"

    effective = _effective_strength(snapshot, overlay)
    if effective <= 0.0:
        return False, "below_min_strength_or_flat"

    if snapshot.direction == "up" and intended_side == "short":
        return True, "ai_trend_up_blocks_short"
    if snapshot.direction == "down" and intended_side == "long":
        return True, "ai_trend_down_blocks_long"
    return False, "side_aligned_with_ai_trend"


def scale_atr_multiplier(
    base: float,
    snapshot: AiTrendSnapshot,
    overlay: AiOverlayConfig,
    position_side: PositionSide,
) -> tuple[float, OverlayDecision]:
    """Phase 2 — return a new ATR multiplier scaled by ai_trend.

    When the trend aligns with the position side, widen the multiplier
    (let the position breathe). When it contradicts, tighten it
    (cut the risk faster). ``flat`` or weak signals → base value.

    The result is bounded by ``overlay.atr_scale_range`` so a buggy
    AI signal can never push the multiplier outside the user's risk
    envelope.
    """
    if not overlay.enabled or not overlay.atr_scaling_enabled:
        return base, OverlayDecision(changed=False, reason="overlay_disabled")

    effective = _effective_strength(snapshot, overlay)
    if effective <= 0.0:
        return base, OverlayDecision(changed=False, reason="below_min_strength_or_flat")

    low, high = overlay.atr_scale_range
    aligned = (snapshot.direction == "up" and position_side == "long") or (
        snapshot.direction == "down" and position_side == "short"
    )
    if aligned:
        factor = 1.0 + effective * (high - 1.0)
        reason = "trend_aligned_widen"
    else:
        factor = 1.0 - effective * (1.0 - low)
        reason = "trend_opposed_tighten"

    scaled = base * factor
    # Defence-in-depth bound: even if factor math drifts, clamp to envelope.
    scaled = max(base * low, min(base * high, scaled))

    if scaled == base:
        return base, OverlayDecision(changed=False, reason="no_op")
    return scaled, OverlayDecision(changed=True, reason=reason)


def scale_rsi_thresholds(
    oversold: int,
    overbought: int,
    snapshot: AiTrendSnapshot,
    overlay: AiOverlayConfig,
) -> tuple[int, int, OverlayDecision]:
    """Phase 3 — shift RSI thresholds symmetrically by ai_trend direction.

    Shifting both thresholds by the same amount preserves the width of the
    neutral band (default 30 to 70 → still 40 points). Otherwise the
    watcher becomes either too noisy or too quiet.

    - ``direction=up``   → shift both up by ``+strength*rsi_max_shift``.
    - ``direction=down`` → shift both down by ``-strength*rsi_max_shift``.
    - ``direction=flat`` or weak → no change.
    """
    if not overlay.enabled or not overlay.rsi_scaling_enabled:
        return oversold, overbought, OverlayDecision(changed=False, reason="overlay_disabled")

    effective = _effective_strength(snapshot, overlay)
    if effective <= 0.0 or overlay.rsi_max_shift == 0:
        return (
            oversold,
            overbought,
            OverlayDecision(changed=False, reason="below_min_strength_or_flat"),
        )

    sign = 1 if snapshot.direction == "up" else -1
    shift = int(round(sign * effective * overlay.rsi_max_shift))
    if shift == 0:
        return (
            oversold,
            overbought,
            OverlayDecision(changed=False, reason="shift_rounded_to_zero"),
        )

    # Clamp to a sensible domain so the watcher never receives e.g. 110.
    new_oversold = max(0, min(100, oversold + shift))
    new_overbought = max(0, min(100, overbought + shift))

    if new_oversold == oversold and new_overbought == overbought:
        return oversold, overbought, OverlayDecision(changed=False, reason="clamped_no_op")

    reason = "trend_up_shift" if sign == 1 else "trend_down_shift"
    return new_oversold, new_overbought, OverlayDecision(changed=True, reason=reason)


# ---------------------------------------------------------------------------
# Watcher-condition string transformer (used by Phase 3 in service.py).
# ---------------------------------------------------------------------------

_COMPARISON_RE = re.compile(r"^\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")
_RANGE_RE = re.compile(
    r"^\s*(between|outside)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


def shift_watcher_condition_threshold(condition: str, shift: int) -> str:
    """Return ``condition`` with its numeric threshold(s) shifted by ``shift``.

    Supports the three watcher-condition shapes used in this codebase
    (see ``app/services/watchers/rule_engine.py``):
    - ``"> 75"`` / ``">= 75"`` / ``"< 30"`` / ``"<= 30"``
    - ``"between 30 60"`` / ``"outside 30 70"``
    - ``"cross_above"`` / ``"cross_below"`` (passed through unchanged)

    Values are clamped to ``[0, 100]`` which is the meaningful range for
    every threshold-comparable indicator the codebase ships with (RSI,
    normalized oscillators). If the condition doesn't match a known
    pattern, the input is returned verbatim so the watcher keeps working.
    """
    if shift == 0:
        return condition

    def _clamp(value: float) -> float:
        return max(0.0, min(100.0, value))

    def _fmt(value: float) -> str:
        return str(int(value)) if value.is_integer() else str(value)

    text = condition.strip()
    cmp_match = _COMPARISON_RE.match(text)
    if cmp_match is not None:
        op_, threshold_raw = cmp_match.groups()
        new_threshold = _clamp(float(threshold_raw) + shift)
        return f"{op_} {_fmt(new_threshold)}"

    range_match = _RANGE_RE.match(text)
    if range_match is not None:
        kind, lower_raw, upper_raw = range_match.groups()
        new_lower = _clamp(float(lower_raw) + shift)
        new_upper = _clamp(float(upper_raw) + shift)
        return f"{kind} {_fmt(new_lower)} {_fmt(new_upper)}"

    return condition
