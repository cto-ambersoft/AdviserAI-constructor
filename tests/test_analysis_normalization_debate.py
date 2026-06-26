from app.core.analysis_normalization import normalize_analysis_payload


def test_normalize_preserves_debate_summary_in_analysis_structured() -> None:
    """C2.5: the debate summary must survive normalization (pass-through)."""
    debate = {
        "applied": True,
        "topology": "directional_risk",
        "winner": "bear",
        "rounds": 2,
        "terminationReason": "converged",
        "confidenceDelta": -0.1,
        "actionChanged": True,
        "durationMs": 1234,
        "recordId": "rec-1",
    }
    payload = {
        "analysisStructured": {
            "bias": "NEUTRAL",
            "confidence": 0.5,
            "keyLevels": {},
            "debate": debate,
        },
        "trendExtraction": {},
        "indicatorRecommendations": None,
    }

    out = normalize_analysis_payload(payload)

    assert out["analysisStructured"]["debate"] == debate


def test_normalize_is_noop_without_debate() -> None:
    payload = {"analysisStructured": {"bias": "BULLISH", "confidence": 0.7}}
    out = normalize_analysis_payload(payload)
    assert "debate" not in out["analysisStructured"]
