"""Unit tests for app.services.auto_trade.ai_overlay.scaler.

These tests cover the pure decision functions: no I/O, no DB. Each test
documents a behavioural contract that the auto-trade integration relies
on, so future refactors stay safe.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas.ai_overlay import AiOverlayConfig, AiTrendSnapshot  # noqa: E402
from app.services.auto_trade.ai_overlay.scaler import (  # noqa: E402
    scale_atr_multiplier,
    scale_rsi_thresholds,
    shift_watcher_condition_threshold,
    should_block_entry,
)


def _snapshot(direction: str, strength: float) -> AiTrendSnapshot:
    return AiTrendSnapshot(
        direction=direction,  # type: ignore[arg-type]
        strength=strength,
        occurred_at_iso="2026-05-12T12:00:00+00:00",
    )


def _overlay(**kwargs) -> AiOverlayConfig:
    base = dict(
        enabled=True,
        entry_side_lock_enabled=True,
        atr_scaling_enabled=True,
        rsi_scaling_enabled=True,
    )
    base.update(kwargs)
    return AiOverlayConfig(**base)


class TestShouldBlockEntry:
    def test_disabled_overlay_never_blocks(self) -> None:
        snap = _snapshot("up", 0.9)
        overlay = _overlay(enabled=False)
        block, reason = should_block_entry("short", snap, overlay)
        assert not block
        assert reason == "overlay_disabled"

    def test_disabled_lock_never_blocks(self) -> None:
        snap = _snapshot("up", 0.9)
        overlay = _overlay(entry_side_lock_enabled=False)
        block, reason = should_block_entry("short", snap, overlay)
        assert not block

    def test_up_blocks_short(self) -> None:
        snap = _snapshot("up", 0.9)
        block, reason = should_block_entry("short", snap, _overlay())
        assert block
        assert reason == "ai_trend_up_blocks_short"

    def test_up_allows_long(self) -> None:
        snap = _snapshot("up", 0.9)
        block, _ = should_block_entry("long", snap, _overlay())
        assert not block

    def test_down_blocks_long(self) -> None:
        snap = _snapshot("down", 0.9)
        block, reason = should_block_entry("long", snap, _overlay())
        assert block
        assert reason == "ai_trend_down_blocks_long"

    def test_down_allows_short(self) -> None:
        snap = _snapshot("down", 0.9)
        block, _ = should_block_entry("short", snap, _overlay())
        assert not block

    def test_flat_never_blocks(self) -> None:
        snap = _snapshot("flat", 0.99)
        block, reason = should_block_entry("long", snap, _overlay())
        assert not block
        assert reason == "below_min_strength_or_flat"

    def test_below_min_strength_never_blocks(self) -> None:
        snap = _snapshot("up", 0.2)  # below default 0.4
        block, _ = should_block_entry("short", snap, _overlay())
        assert not block


class TestScaleAtrMultiplier:
    def test_disabled_overlay_returns_base(self) -> None:
        snap = _snapshot("up", 1.0)
        overlay = _overlay(enabled=False)
        scaled, decision = scale_atr_multiplier(2.0, snap, overlay, "long")
        assert scaled == 2.0
        assert not decision.changed

    def test_disabled_atr_returns_base(self) -> None:
        snap = _snapshot("up", 1.0)
        overlay = _overlay(atr_scaling_enabled=False)
        scaled, decision = scale_atr_multiplier(2.0, snap, overlay, "long")
        assert scaled == 2.0
        assert not decision.changed

    def test_flat_returns_base(self) -> None:
        snap = _snapshot("flat", 1.0)
        scaled, decision = scale_atr_multiplier(2.0, snap, _overlay(), "long")
        assert scaled == 2.0
        assert not decision.changed

    def test_aligned_up_long_widens(self) -> None:
        # strength=1.0 → factor = 1.0 + 1.0 * (1.2 - 1.0) = 1.2
        snap = _snapshot("up", 1.0)
        scaled, decision = scale_atr_multiplier(2.0, snap, _overlay(), "long")
        assert scaled == pytest.approx(2.4)
        assert decision.changed
        assert decision.reason == "trend_aligned_widen"

    def test_aligned_down_short_widens(self) -> None:
        snap = _snapshot("down", 1.0)
        scaled, decision = scale_atr_multiplier(2.0, snap, _overlay(), "short")
        assert scaled == pytest.approx(2.4)
        assert decision.reason == "trend_aligned_widen"

    def test_opposed_up_short_tightens(self) -> None:
        # strength=1.0 → factor = 1.0 - 1.0 * (1.0 - 0.8) = 0.8
        snap = _snapshot("up", 1.0)
        scaled, decision = scale_atr_multiplier(2.0, snap, _overlay(), "short")
        assert scaled == pytest.approx(1.6)
        assert decision.reason == "trend_opposed_tighten"

    def test_partial_strength_partial_shift(self) -> None:
        # strength=0.5 with min_strength=0.4 → factor = 1.0 + 0.5 * 0.2 = 1.10
        snap = _snapshot("up", 0.5)
        scaled, decision = scale_atr_multiplier(2.0, snap, _overlay(), "long")
        assert scaled == pytest.approx(2.2)
        assert decision.changed

    def test_weak_strength_no_op(self) -> None:
        snap = _snapshot("up", 0.1)
        scaled, decision = scale_atr_multiplier(2.0, snap, _overlay(), "long")
        assert scaled == 2.0
        assert not decision.changed

    def test_bounds_enforced(self) -> None:
        # Custom narrow envelope: even strength=1 can only move within ±5 %.
        overlay = _overlay(atr_scale_range=(0.95, 1.05))
        snap = _snapshot("up", 1.0)
        scaled, _ = scale_atr_multiplier(2.0, snap, overlay, "long")
        assert 2.0 * 0.95 <= scaled <= 2.0 * 1.05


class TestScaleRsiThresholds:
    def test_disabled_returns_base(self) -> None:
        snap = _snapshot("up", 1.0)
        overlay = _overlay(rsi_scaling_enabled=False)
        os_, ob_, decision = scale_rsi_thresholds(30, 70, snap, overlay)
        assert (os_, ob_) == (30, 70)
        assert not decision.changed

    def test_flat_returns_base(self) -> None:
        snap = _snapshot("flat", 1.0)
        os_, ob_, decision = scale_rsi_thresholds(30, 70, snap, _overlay())
        assert (os_, ob_) == (30, 70)
        assert not decision.changed

    def test_up_shifts_both_up(self) -> None:
        # rsi_max_shift=5, strength=1.0 → shift = +5.
        snap = _snapshot("up", 1.0)
        os_, ob_, decision = scale_rsi_thresholds(30, 70, snap, _overlay())
        assert (os_, ob_) == (35, 75)
        assert decision.reason == "trend_up_shift"
        # Width preserved
        assert (ob_ - os_) == (70 - 30)

    def test_down_shifts_both_down(self) -> None:
        snap = _snapshot("down", 1.0)
        os_, ob_, decision = scale_rsi_thresholds(30, 70, snap, _overlay())
        assert (os_, ob_) == (25, 65)
        assert decision.reason == "trend_down_shift"

    def test_partial_strength_partial_shift(self) -> None:
        # strength=0.6, shift=5 → +3.
        snap = _snapshot("up", 0.6)
        os_, ob_, decision = scale_rsi_thresholds(30, 70, snap, _overlay())
        assert (os_, ob_) == (33, 73)
        assert decision.changed

    def test_zero_max_shift_returns_base(self) -> None:
        snap = _snapshot("up", 1.0)
        overlay = _overlay(rsi_max_shift=0)
        os_, ob_, decision = scale_rsi_thresholds(30, 70, snap, overlay)
        assert (os_, ob_) == (30, 70)
        assert not decision.changed

    def test_clamping_at_domain_boundaries(self) -> None:
        # Pathological base values + strong up shift: still within 0..100.
        snap = _snapshot("up", 1.0)
        overlay = _overlay(rsi_max_shift=20)
        os_, ob_, _ = scale_rsi_thresholds(85, 99, snap, overlay)
        assert 0 <= os_ <= 100
        assert 0 <= ob_ <= 100


class TestShiftWatcherConditionThreshold:
    @pytest.mark.parametrize(
        "input_condition,shift,expected",
        [
            ("> 75", 5, "> 80"),
            (">= 30", -5, ">= 25"),
            ("< 30", -5, "< 25"),
            ("<= 70", 3, "<= 73"),
            ("between 30 60", 5, "between 35 65"),
            ("outside 30 70", -5, "outside 25 65"),
            (" > 75 ", 5, "> 80"),  # whitespace tolerance
            ("> 75.5", 1, "> 76.5"),  # float threshold
        ],
    )
    def test_shifts_threshold_values(
        self, input_condition: str, shift: int, expected: str
    ) -> None:
        assert shift_watcher_condition_threshold(input_condition, shift) == expected

    def test_zero_shift_returns_unchanged(self) -> None:
        assert shift_watcher_condition_threshold("> 75", 0) == "> 75"

    @pytest.mark.parametrize("cross", ["cross_above", "cross_below"])
    def test_cross_conditions_pass_through(self, cross: str) -> None:
        assert shift_watcher_condition_threshold(cross, 5) == cross

    def test_unknown_pattern_pass_through(self) -> None:
        # Garbage in → garbage out (but not crash): preserve original.
        assert shift_watcher_condition_threshold("foo bar baz", 5) == "foo bar baz"

    def test_clamps_at_domain_boundaries(self) -> None:
        # Aggressive shift cannot push threshold above 100 or below 0.
        assert shift_watcher_condition_threshold("> 95", 50) == "> 100"
        assert shift_watcher_condition_threshold("< 5", -50) == "< 0"


# ---------------------------------------------------------------------------
# W2: resolver envelope parsing — decisionEventId + reasoningPath extraction.
# ---------------------------------------------------------------------------

from app.services.auto_trade.ai_overlay.resolver import _extract_ai_trend  # noqa: E402


class TestExtractAiTrend:
    def test_flat_envelope_with_full_payload(self) -> None:
        result = _extract_ai_trend(
            {
                "aiTrend": {"direction": "up", "strength": 0.7},
                "decisionEventId": "evt-1",
                "reasoningPath": [
                    {
                        "agentKey": "twitterSentiment",
                        "signal": "up",
                        "confidence": 0.8,
                        "weight": 0.3,
                    }
                ],
            }
        )
        assert result is not None
        direction, strength, event_id, reasoning = result
        assert direction == "up"
        assert strength == 0.7
        assert event_id == "evt-1"
        assert len(reasoning) == 1
        assert reasoning[0].agent_key == "twitterSentiment"

    def test_nested_under_result_json(self) -> None:
        result = _extract_ai_trend(
            {
                "result_json": {
                    "aiTrend": {"direction": "down", "strength": 0.55},
                    "decisionEventId": "evt-2",
                }
            }
        )
        assert result is not None
        direction, strength, event_id, _ = result
        assert direction == "down"
        assert strength == 0.55
        assert event_id == "evt-2"

    def test_snake_case_keys_accepted(self) -> None:
        result = _extract_ai_trend(
            {
                "ai_trend": {"direction": "flat", "strength": 0.1},
                "decision_event_id": "evt-snake",
                "reasoning_path": [
                    {"agent_key": "techModelSignal", "signal": "flat", "weight": 0.2}
                ],
            }
        )
        assert result is not None
        _, _, event_id, reasoning = result
        assert event_id == "evt-snake"
        assert reasoning[0].agent_key == "techModelSignal"

    def test_missing_decision_event_id_returns_none(self) -> None:
        # Legacy record: ai_trend present but no traceability fields.
        result = _extract_ai_trend(
            {"aiTrend": {"direction": "up", "strength": 0.5}}
        )
        assert result is not None
        _, _, event_id, reasoning = result
        assert event_id is None
        assert reasoning == []

    def test_malformed_reasoning_path_skipped(self) -> None:
        result = _extract_ai_trend(
            {
                "aiTrend": {"direction": "up", "strength": 0.5},
                "reasoningPath": [
                    "not_a_dict",
                    {},  # all fields None — useless entry, must skip
                    {"agentKey": "valid", "signal": "up"},
                ],
            }
        )
        assert result is not None
        _, _, _, reasoning = result
        # Only the valid entry survives.
        assert len(reasoning) == 1
        assert reasoning[0].agent_key == "valid"

    def test_no_ai_trend_returns_none(self) -> None:
        assert _extract_ai_trend({"foo": "bar"}) is None
        assert _extract_ai_trend({}) is None
        assert _extract_ai_trend(None) is None

    def test_invalid_direction_returns_none(self) -> None:
        assert (
            _extract_ai_trend(
                {"aiTrend": {"direction": "diagonal", "strength": 0.5}}
            )
            is None
        )

    def test_out_of_range_strength_returns_none(self) -> None:
        assert (
            _extract_ai_trend({"aiTrend": {"direction": "up", "strength": 1.5}})
            is None
        )
