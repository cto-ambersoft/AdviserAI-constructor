"""AI Trend Overlay configuration schemas.

W4 of Milestone 4: per-user opt-in overlay that consumes ``ai_trend`` from
the freshest ``personal_analysis_history`` record and applies it as a
bounded scaler to live auto-trade parameters (entry-side lock, ATR
multiplier, RSI thresholds).

The overlay is intentionally opt-in and bounded — base parameters remain
the anchor, ai_trend can only nudge them inside ``atr_scale_range`` /
``rsi_max_shift``. This protects against noisy or buggy AI signals.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


AiTrendDirection = Literal["up", "down", "flat"]


class AiOverlayConfig(BaseModel):
    """Per-user overlay configuration stored as JSON on AutoTradeConfig.

    All flags default to ``False`` so existing users see no behaviour
    change until they explicitly opt in. ``stale_max_minutes`` matches the
    plan-mandated 4-hour freshness ceiling for ai_trend.
    """

    enabled: bool = Field(default=False, description="Global overlay kill-switch.")
    entry_side_lock_enabled: bool = Field(
        default=False,
        description="Block entries whose side contradicts ai_trend direction (phase 1).",
    )
    atr_scaling_enabled: bool = Field(
        default=False,
        description="Scale the SL ATR multiplier by ai_trend.strength (phase 2).",
    )
    rsi_scaling_enabled: bool = Field(
        default=False,
        description="Shift RSI watcher thresholds by ai_trend direction (phase 3).",
    )
    stale_max_minutes: int = Field(
        default=240,
        ge=1,
        le=1440,
        description="Maximum age of the ai_trend record (minutes). Older records trigger fail-open fallback.",
    )
    min_strength: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="Minimum ai_trend.strength below which the overlay treats the signal as no-op.",
    )
    atr_scale_range: tuple[float, float] = Field(
        default=(0.8, 1.2),
        description="(min, max) multiplier applied to the base ATR multiplier. Bounds the runtime override.",
    )
    rsi_max_shift: int = Field(
        default=5,
        ge=0,
        le=20,
        description="Maximum absolute shift (in points) applied to RSI thresholds by the overlay.",
    )

    @model_validator(mode="after")
    def _check_atr_range(self) -> Self:
        low, high = self.atr_scale_range
        if not (0.1 <= low <= 1.0 <= high <= 3.0):
            raise ValueError(
                "atr_scale_range must satisfy 0.1 <= low <= 1.0 <= high <= 3.0 (current: %s)."
                % (self.atr_scale_range,)
            )
        return self

    @classmethod
    def from_record(cls, raw: dict[str, Any] | None) -> AiOverlayConfig:
        """Parse a JSON record from DB into a config, returning defaults for NULL/missing."""
        if not raw:
            return cls()
        return cls.model_validate(raw)

    def to_record(self) -> dict[str, Any]:
        """Serialise back into JSON-friendly form (tuples → lists)."""
        return self.model_dump(mode="json")


class AiReasoningEntry(BaseModel):
    """Single agent's contribution to a decision event.

    Mirrors ``ReasoningPathEntry`` from core's decision-analytics. All
    fields are optional because the upstream envelope occasionally drops
    one (e.g. an agent that errored mid-run): we want the snapshot to
    survive partial data rather than reject it wholesale.
    """

    agent_key: str | None = None
    signal: str | None = None
    confidence: float | None = None
    weight: float | None = None
    summary: str | None = None

    def to_compact_payload(self) -> dict[str, Any]:
        """Return a JSON-safe dict for audit-payload denormalisation.

        Strips None-valued keys to keep the persisted JSON minimal.
        """
        out: dict[str, Any] = {}
        if self.agent_key is not None:
            out["agent_key"] = self.agent_key
        if self.signal is not None:
            out["signal"] = self.signal
        if self.confidence is not None:
            out["confidence"] = self.confidence
        if self.weight is not None:
            out["weight"] = self.weight
        if self.summary is not None:
            out["summary"] = self.summary
        return out


class AiTrendSnapshot(BaseModel):
    """Resolved ai_trend datapoint passed to the scaler.

    Mirrors ``AiTrend`` from core's decision-analytics, plus the
    ``occurred_at`` timestamp used for freshness checks. The optional
    ``decision_event_id`` and ``reasoning_path`` make every overlay
    decision traceable back to the exact AI decision document in core
    (W2 last-mile traceability).
    """

    direction: AiTrendDirection
    strength: float = Field(ge=0.0, le=1.0)
    occurred_at_iso: str
    source: Literal["personal_analysis_history", "fallback"] = "personal_analysis_history"
    decision_event_id: str | None = None
    reasoning_path: list[AiReasoningEntry] = Field(default_factory=list)


class AiOverlayConfigResponse(BaseModel):
    """API response wrapper."""

    config: AiOverlayConfig


class AiOverlayConfigUpdateRequest(BaseModel):
    """API request — partial update; missing fields keep their current value."""

    enabled: bool | None = None
    entry_side_lock_enabled: bool | None = None
    atr_scaling_enabled: bool | None = None
    rsi_scaling_enabled: bool | None = None
    stale_max_minutes: int | None = Field(default=None, ge=1, le=1440)
    min_strength: float | None = Field(default=None, ge=0.0, le=1.0)
    atr_scale_range: tuple[float, float] | None = None
    rsi_max_shift: int | None = Field(default=None, ge=0, le=20)

    def merge_into(self, current: AiOverlayConfig) -> AiOverlayConfig:
        """Apply non-None fields from this request onto ``current``."""
        payload = current.model_dump()
        for field_name, value in self.model_dump(exclude_unset=True).items():
            if value is not None:
                payload[field_name] = value
        return AiOverlayConfig.model_validate(payload)
