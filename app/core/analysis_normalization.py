from typing import Any

_TREND_FLAT_KEY = "flat"
_TREND_NEUTRAL_KEY = "neutral"
_BIAS_NEUTRAL_VALUES = {"NEUTRAL", "FLAT"}


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_probability_pct(value: Any) -> float | None:
    parsed = _to_float(value)
    if parsed is None:
        return None
    if parsed <= 1.0:
        parsed *= 100.0
    if parsed < 0:
        return 0.0
    if parsed > 100:
        return 100.0
    return parsed


def _is_missing_probability(value: Any) -> bool:
    parsed = _to_float(value)
    return parsed is None or parsed <= 0


def _is_missing_price_level(value: Any) -> bool:
    parsed = _to_float(value)
    return parsed is None or parsed <= 0


def _coalesce_price_level(source: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        parsed = _to_float(source.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _normalize_trend_blocks(container: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(container)
    neutral_block = normalized.get(_TREND_NEUTRAL_KEY)
    flat_block = normalized.get(_TREND_FLAT_KEY)

    if isinstance(flat_block, dict):
        base = dict(flat_block)
    elif isinstance(neutral_block, dict):
        base = dict(neutral_block)
    else:
        base = {}

    if isinstance(neutral_block, dict):
        for key, value in neutral_block.items():
            base.setdefault(key, value)
    if isinstance(flat_block, dict):
        for key, value in flat_block.items():
            base[key] = value

    normalized[_TREND_FLAT_KEY] = base
    return normalized


def normalize_analysis_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    analysis_structured = normalized.get("analysisStructured")
    trend_extraction = normalized.get("trendExtraction")
    indicator_recommendations = normalized.get("indicatorRecommendations")

    if not isinstance(indicator_recommendations, dict):
        normalized["indicatorRecommendations"] = None

    if isinstance(trend_extraction, dict):
        normalized_trend = _normalize_trend_blocks(trend_extraction)
        flat = normalized_trend.get(_TREND_FLAT_KEY)
        if not isinstance(flat, dict):
            flat = {}
            normalized_trend[_TREND_FLAT_KEY] = flat

        if isinstance(analysis_structured, dict):
            bias = str(analysis_structured.get("bias") or "").strip().upper()
            if bias in _BIAS_NEUTRAL_VALUES:
                if _is_missing_probability(flat.get("probabilityPct")):
                    confidence_pct = _normalize_probability_pct(
                        analysis_structured.get("confidence")
                    )
                    if confidence_pct is not None:
                        flat["probabilityPct"] = confidence_pct

                key_levels = analysis_structured.get("keyLevels")
                if isinstance(key_levels, dict):
                    if _is_missing_price_level(flat.get("takeProfit")):
                        take_profit = _coalesce_price_level(
                            key_levels,
                            ("resistance", "strongResistance", "support", "strongSupport"),
                        )
                        if take_profit is not None:
                            flat["takeProfit"] = take_profit
                    if _is_missing_price_level(flat.get("stopLoss")):
                        stop_loss = _coalesce_price_level(
                            key_levels,
                            ("support", "strongSupport", "resistance", "strongResistance"),
                        )
                        if stop_loss is not None:
                            flat["stopLoss"] = stop_loss

        normalized["trendExtraction"] = normalized_trend

    if isinstance(indicator_recommendations, dict):
        normalized["indicatorRecommendations"] = _normalize_trend_blocks(indicator_recommendations)

    return normalized
