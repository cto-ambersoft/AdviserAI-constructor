from datetime import UTC, datetime

import pytest

from app.services.auto_trade.signal import (
    adapt_legacy_analysis_structured_payload,
    parse_auto_trade_signal,
    symbol_market_key,
    to_bybit_linear_symbol,
)


def _strict_payload(confidence_pct: float | int | str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "symbol": "BTCUSDT",
        "trend": "LONG",
        "confidence_pct": confidence_pct,
        "price": {"current": 100_000.0},
        "generated_at": "2026-05-11T12:00:00+00:00",
    }


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


# ─── strict-contract confidence_pct normalization (symmetry fix) ──────────


@pytest.mark.parametrize(
    "raw,expected_pct",
    [
        (0.65, 65.0),    # fraction from AI core → normalized to percent
        (0.999, 99.9),   # high-fraction → near-100 percent
        (1.0, 100.0),    # boundary: 1.0 is treated as max-percent (100 %)
        (65.0, 65.0),    # percent already → unchanged
        (65, 65.0),      # integer percent → unchanged
        (100.0, 100.0),  # upper bound preserved
        (0, 0.0),        # zero left at zero (will be blocked by the gate anyway)
        ("0.62", 62.0),  # JSON often arrives as string → still normalized
        ("62", 62.0),
    ],
)
def test_parse_auto_trade_signal_normalizes_strict_contract_confidence_pct(
    raw: float | int | str,
    expected_pct: float,
) -> None:
    """Regression: strict-contract path must apply the same fraction→percent
    rule as the legacy adapter. Without this, an AI-core signal with
    ``confidence_pct: 0.65`` lands as the literal 0.65 in the gate and the
    threshold comparison loses all meaning.
    """
    parsed = parse_auto_trade_signal(_strict_payload(raw))
    assert parsed.confidence_pct == pytest.approx(expected_pct)


def test_parse_auto_trade_signal_rejects_out_of_range_confidence_pct() -> None:
    with pytest.raises(ValueError, match="confidence_pct"):
        parse_auto_trade_signal(_strict_payload(150.0))
    with pytest.raises(ValueError, match="confidence_pct"):
        parse_auto_trade_signal(_strict_payload(-5.0))


def test_parse_auto_trade_signal_legacy_and_strict_paths_agree_on_units() -> None:
    """Both entry parsers must produce the same percent value for the
    same logical input (a fraction of 0.65). This is the property the
    "unit asymmetry" defect violated."""
    legacy_adapted = adapt_legacy_analysis_structured_payload(
        payload={
            "analysisStructured": {
                "symbol": "BTCUSDT",
                "bias": "BULLISH",
                "confidence": 0.65,
                "currentPrice": 100_000.0,
                "timestamp": "2026-05-11T12:00:00Z",
            }
        },
        history_symbol="BTC",
        core_completed_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )
    strict_parsed = parse_auto_trade_signal(_strict_payload(0.65))
    legacy_parsed = parse_auto_trade_signal(legacy_adapted)
    assert legacy_parsed.confidence_pct == strict_parsed.confidence_pct == pytest.approx(65.0)
