from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

AiSideLock = Literal["long", "short", "none"]
AI_REQUIRED_COLUMNS = {
    "signal_time_utc",
    "predicted_trend",
    "confidence_bull",
    "confidence_bear",
    "confidence_flat",
}


@dataclass(frozen=True)
class AiForecastOverlay:
    regimes: list[str]
    active: list[bool]
    applied: list[bool]
    signal_times: list[str | None]
    horizon_end_times: list[str | None]


def normalize_ai_regime(value: object) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"bull", "bullish", "up", "long"}:
        return "Bull"
    if normalized in {"bear", "bearish", "down", "short"}:
        return "Bear"
    return "Flat"


def prepare_ai_forecast_frame(ai_rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(ai_rows)
    missing = sorted(AI_REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"AI forecast rows missing required fields: {', '.join(missing)}")
    frame["signal_time_utc"] = pd.to_datetime(frame["signal_time_utc"], utc=True, errors="coerce")
    if frame["signal_time_utc"].isna().any():
        raise ValueError("AI forecast contains invalid signal_time_utc values.")
    frame = frame.sort_values("signal_time_utc").reset_index(drop=True)
    frame["predicted_regime"] = frame["predicted_trend"].map(normalize_ai_regime)
    frame["confidence_bull"] = pd.to_numeric(frame["confidence_bull"], errors="coerce")
    frame["confidence_bear"] = pd.to_numeric(frame["confidence_bear"], errors="coerce")
    frame["confidence_flat"] = pd.to_numeric(frame["confidence_flat"], errors="coerce")
    if "horizon_end_utc" in frame.columns:
        raw_horizon = frame["horizon_end_utc"]
        frame["horizon_end_utc"] = pd.to_datetime(raw_horizon, utc=True, errors="coerce")
        invalid_horizon = raw_horizon.notna() & frame["horizon_end_utc"].isna()
        if invalid_horizon.any():
            raise ValueError("AI forecast contains invalid horizon_end_utc values.")
    else:
        frame["horizon_end_utc"] = pd.NaT
    return frame[
        [
            "signal_time_utc",
            "horizon_end_utc",
            "predicted_regime",
            "confidence_bull",
            "confidence_bear",
            "confidence_flat",
        ]
    ]


def resolve_ai_forecast_overlay_per_bar(
    market_index: pd.Index,
    ai_rows: list[dict[str, Any]],
    fallback_regime: str,
    bull_threshold: float,
    bear_threshold: float,
) -> AiForecastOverlay:
    if not ai_rows:
        raise ValueError("AI forecast rows are empty.")
    ai_frame = prepare_ai_forecast_frame(ai_rows)
    market_times = pd.DataFrame(
        {
            "bar_time": pd.to_datetime(market_index, utc=True),
            "_position": np.arange(len(market_index)),
        }
    )
    joined = pd.merge_asof(
        market_times.sort_values("bar_time"),
        ai_frame,
        left_on="bar_time",
        right_on="signal_time_utc",
        direction="backward",
    ).sort_values("_position")
    fallback = normalize_ai_regime(fallback_regime)
    predicted = joined["predicted_regime"]
    bull_conf = pd.to_numeric(joined["confidence_bull"], errors="coerce")
    bear_conf = pd.to_numeric(joined["confidence_bear"], errors="coerce")
    flat_conf = pd.to_numeric(joined["confidence_flat"], errors="coerce")
    signal_times = pd.to_datetime(joined["signal_time_utc"], utc=True, errors="coerce")
    horizon_times = pd.to_datetime(joined["horizon_end_utc"], utc=True, errors="coerce")

    resolved = np.full(len(joined), fallback, dtype=object)
    active_mask = signal_times.notna() & (horizon_times.isna() | joined["bar_time"].le(horizon_times))
    bull_mask = predicted.eq("Bull") & bull_conf.ge(bull_threshold)
    bear_mask = predicted.eq("Bear") & bear_conf.ge(bear_threshold)
    flat_threshold = max(float(bull_threshold), float(bear_threshold))
    flat_mask = predicted.eq("Flat") & flat_conf.ge(flat_threshold)
    applied_mask = active_mask & (bull_mask | bear_mask | flat_mask)

    resolved[(active_mask & bull_mask).to_numpy()] = "Bull"
    resolved[(active_mask & bear_mask).to_numpy()] = "Bear"
    resolved[(active_mask & flat_mask).to_numpy()] = "Flat"

    def _iso_or_none(value: object) -> str | None:
        ts = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.isoformat()

    return AiForecastOverlay(
        regimes=[str(item) for item in resolved.tolist()],
        active=[bool(item) for item in active_mask.tolist()],
        applied=[bool(item) for item in applied_mask.tolist()],
        signal_times=[_iso_or_none(value) for value in joined["signal_time_utc"].tolist()],
        horizon_end_times=[_iso_or_none(value) for value in joined["horizon_end_utc"].tolist()],
    )


def resolve_ai_regimes_per_bar(
    market_index: pd.Index,
    ai_rows: list[dict[str, Any]],
    fallback_regime: str,
    bull_threshold: float,
    bear_threshold: float,
) -> list[str]:
    return resolve_ai_forecast_overlay_per_bar(
        market_index=market_index,
        ai_rows=ai_rows,
        fallback_regime=fallback_regime,
        bull_threshold=bull_threshold,
        bear_threshold=bear_threshold,
    ).regimes


def resolve_ai_side_locks_per_bar(
    market_index: pd.Index,
    ai_rows: list[dict[str, Any]],
    bull_threshold: float,
    bear_threshold: float,
) -> list[AiSideLock]:
    overlay = resolve_ai_forecast_overlay_per_bar(
        market_index=market_index,
        ai_rows=ai_rows,
        fallback_regime="Flat",
        bull_threshold=bull_threshold,
        bear_threshold=bear_threshold,
    )
    locks: list[AiSideLock] = []
    for regime, applied in zip(overlay.regimes, overlay.applied, strict=False):
        if regime == "Bull" and applied:
            locks.append("short")
        elif regime == "Bear" and applied:
            locks.append("long")
        else:
            locks.append("none")
    return locks


def resolve_ai_risk_multiplier_per_bar(
    market_index: pd.Index,
    ai_rows: list[dict[str, Any]],
    bull_threshold: float,
    bear_threshold: float,
) -> list[float]:
    overlay = resolve_ai_forecast_overlay_per_bar(
        market_index=market_index,
        ai_rows=ai_rows,
        fallback_regime="Flat",
        bull_threshold=bull_threshold,
        bear_threshold=bear_threshold,
    )
    return [
        0.5 if regime == "Flat" and applied else 1.0
        for regime, applied in zip(overlay.regimes, overlay.applied, strict=False)
    ]


def side_allowed(side: str, lock: AiSideLock | None) -> bool:
    normalized = side.lower()
    if lock == "long" and normalized == "long":
        return False
    if lock == "short" and normalized == "short":
        return False
    return True
