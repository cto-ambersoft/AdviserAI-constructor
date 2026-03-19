from datetime import UTC, datetime

from app.services.auto_trade.signal import (
    adapt_legacy_analysis_structured_payload,
    parse_auto_trade_signal,
    symbol_market_key,
    to_bybit_linear_symbol,
)


def test_symbol_market_key_appends_default_usdt_for_base_asset() -> None:
    assert symbol_market_key("BTC") == "BTCUSDT"
    assert symbol_market_key("eth") == "ETHUSDT"


def test_to_bybit_linear_symbol_appends_default_usdt_for_base_asset() -> None:
    assert to_bybit_linear_symbol("BTC") == "BTC/USDT:USDT"
    assert to_bybit_linear_symbol("eth") == "ETH/USDT:USDT"


def test_adapt_legacy_analysis_structured_payload_maps_to_strict_contract() -> None:
    payload = {
        "analysisStructured": {
            "symbol": "BTCUSDT",
            "bias": "BULLISH",
            "confidence": 0.48,
            "currentPrice": 67210.8,
            "timestamp": "2026-03-09T07:11:47.097Z",
        }
    }
    adapted = adapt_legacy_analysis_structured_payload(
        payload=payload,
        history_symbol="BTC",
        core_completed_at=datetime(2026, 3, 9, 7, 12, tzinfo=UTC),
    )
    parsed = parse_auto_trade_signal(adapted)
    assert parsed.schema_version == "legacy-analysisStructured-v1"
    assert parsed.symbol == "BTCUSDT"
    assert parsed.trend == "LONG"
    assert parsed.confidence_pct == 48.0
    assert parsed.price_current == 67210.8
    assert parsed.generated_at.isoformat().startswith("2026-03-09T07:11:47.097")


def test_adapt_legacy_analysis_structured_payload_uses_fallbacks() -> None:
    completed_at = datetime(2026, 3, 9, 7, 12, tzinfo=UTC)
    payload = {
        "analysisStructured": {
            "bias": "NEUTRAL",
            "confidence": 72,
            "currentPrice": 68000,
        }
    }
    adapted = adapt_legacy_analysis_structured_payload(
        payload=payload,
        history_symbol="BTC",
        core_completed_at=completed_at,
    )
    parsed = parse_auto_trade_signal(adapted)
    assert parsed.symbol == "BTC"
    assert parsed.trend == "NEUTRAL"
    assert parsed.confidence_pct == 72.0
    assert parsed.generated_at == completed_at
