import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

SignalTrend = Literal["LONG", "SHORT", "NEUTRAL"]
_KNOWN_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH", "EUR", "BUSD")
_DEFAULT_QUOTE = "USDT"


@dataclass(slots=True)
class ParsedAutoTradeSignal:
    schema_version: str
    symbol: str
    trend: SignalTrend
    confidence_pct: float
    price_current: float
    generated_at: datetime


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _normalize_trend_from_bias(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if normalized in {"LONG", "SHORT", "NEUTRAL"}:
        return normalized
    mapping = {
        "BULLISH": "LONG",
        "BEARISH": "SHORT",
        "FLAT": "NEUTRAL",
    }
    return mapping.get(normalized)


def _normalize_confidence_pct(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 1.0:
        parsed *= 100.0
    return parsed


def adapt_legacy_analysis_structured_payload(
    *,
    payload: dict[str, Any],
    history_symbol: str | None,
    core_completed_at: datetime | None,
    history_created_at: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    has_strict_contract = all(
        key in payload
        for key in (
            "schema_version",
            "symbol",
            "trend",
            "confidence_pct",
            "price",
            "generated_at",
        )
    )
    if has_strict_contract:
        return dict(payload)

    analysis_structured = payload.get("analysisStructured")
    if not isinstance(analysis_structured, dict):
        return dict(payload)

    adapted: dict[str, Any] = {
        "schema_version": "legacy-analysisStructured-v1",
        "symbol": analysis_structured.get("symbol") or history_symbol,
        "trend": _normalize_trend_from_bias(analysis_structured.get("bias")),
        "confidence_pct": _normalize_confidence_pct(analysis_structured.get("confidence")),
        "price": {"current": analysis_structured.get("currentPrice")},
        "generated_at": (
            analysis_structured.get("timestamp")
            or _datetime_to_iso(core_completed_at)
            or _datetime_to_iso(history_created_at)
        ),
    }
    return adapted


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("generated_at must be a non-empty ISO timestamp string.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at must be a valid ISO timestamp.") from exc


def symbol_market_key(symbol: str) -> str:
    raw = symbol.strip().upper()
    if not raw:
        raise ValueError("symbol is required.")

    # CCXT-like pair: BTC/USDT or BTC/USDT:USDT -> BTCUSDT
    if "/" in raw:
        base, quote_part = raw.split("/", 1)
        quote = quote_part.split(":", 1)[0].strip()
        base = base.strip()
        if not base or not quote:
            raise ValueError("symbol must include base and quote asset.")
        return f"{base}{quote}"

    compact = re.sub(r"[^A-Z0-9]", "", raw)
    if not compact:
        raise ValueError("symbol must include alphanumeric assets.")

    for quote in _KNOWN_QUOTES:
        if compact.endswith(quote) and len(compact) > len(quote):
            return compact
    if compact.isalpha() and len(compact) >= 2 and len(compact) <= 10:
        return f"{compact}{_DEFAULT_QUOTE}"
    raise ValueError("symbol must include a supported quote asset.")


def to_linear_perp_symbol(symbol: str) -> str:
    raw = symbol.strip().upper()
    if not raw:
        raise ValueError("symbol is required.")

    if "/" in raw:
        base, quote_part = raw.split("/", 1)
        quote = quote_part.split(":", 1)[0].strip()
        base = base.strip()
        if not base or not quote:
            raise ValueError("symbol must include base and quote asset.")
        return f"{base}/{quote}:{quote}"

    compact = re.sub(r"[^A-Z0-9]", "", raw)
    for quote in _KNOWN_QUOTES:
        if compact.endswith(quote) and len(compact) > len(quote):
            base = compact[: -len(quote)]
            return f"{base}/{quote}:{quote}"
    if compact.isalpha() and len(compact) >= 2 and len(compact) <= 10:
        return f"{compact}/{_DEFAULT_QUOTE}:{_DEFAULT_QUOTE}"
    raise ValueError("symbol must include a supported quote asset.")


def to_bybit_linear_symbol(symbol: str) -> str:
    # Backward-compatible alias used across existing auto-trade code/tests.
    return to_linear_perp_symbol(symbol)


def to_chart_symbol(symbol: str) -> str:
    normalized = to_linear_perp_symbol(symbol)
    base, quote_part = normalized.split("/", 1)
    quote = quote_part.split(":", 1)[0]
    return f"{base}/{quote}"


def parse_auto_trade_signal(payload: dict[str, Any]) -> ParsedAutoTradeSignal:
    if not isinstance(payload, dict):
        raise ValueError("result_json must be an object.")

    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise ValueError("schema_version is required.")

    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol is required.")

    trend_raw = payload.get("trend")
    if not isinstance(trend_raw, str):
        raise ValueError("trend is required.")
    trend = trend_raw.strip().upper()
    if trend not in {"LONG", "SHORT", "NEUTRAL"}:
        raise ValueError("trend must be one of LONG, SHORT, NEUTRAL.")

    confidence_raw = payload.get("confidence_pct")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float, str)):
        raise ValueError("confidence_pct must be a number in [0, 100].")
    try:
        confidence_pct = float(confidence_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence_pct must be a number in [0, 100].") from exc
    if confidence_pct < 0 or confidence_pct > 100:
        raise ValueError("confidence_pct must be in range [0, 100].")

    price_obj = payload.get("price")
    if not isinstance(price_obj, dict):
        raise ValueError("price object with current is required.")
    current_price_raw = price_obj.get("current")
    if isinstance(current_price_raw, bool) or not isinstance(current_price_raw, (int, float, str)):
        raise ValueError("price.current must be a positive number.")
    try:
        price_current = float(current_price_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("price.current must be a positive number.") from exc
    if price_current <= 0:
        raise ValueError("price.current must be a positive number.")

    generated_at = _parse_datetime(payload.get("generated_at"))
    return ParsedAutoTradeSignal(
        schema_version=schema_version.strip(),
        symbol=symbol.strip(),
        trend=cast(SignalTrend, trend),
        confidence_pct=confidence_pct,
        price_current=price_current,
        generated_at=generated_at,
    )
