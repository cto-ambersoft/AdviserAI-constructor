from app.services.backtesting.vwap_builder import resolve_enabled_indicators


def test_resolve_enabled_indicators_prefers_explicit_enabled() -> None:
    params = {
        "preset": "Trend",
        "enabled": ["VWAP", "MACD", "ATR"],
    }
    resolved = resolve_enabled_indicators(params)
    assert resolved == {"VWAP", "MACD", "ATR"}


def test_resolve_enabled_indicators_falls_back_to_preset() -> None:
    params = {"preset": "Breakdown", "enabled": []}
    resolved = resolve_enabled_indicators(params)
    assert resolved == {"VWAP", "MACD", "ADX", "ATR", "Volume SMA"}
