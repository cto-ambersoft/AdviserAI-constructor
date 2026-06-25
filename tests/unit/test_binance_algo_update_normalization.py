"""Regression: Binance ALGO_UPDATE WS event must extract algo-specific fields.

Per Binance USDS-M Futures docs (Event Algo Order Update), the nested object
under ``payload["o"]`` uses *different* field codes from a regular
``ORDER_TRADE_UPDATE``:

  * ``aid``  — algo id (the value to use for cancel/lookup)
  * ``caid`` — client algo id
  * ``tp``   — trigger price
  * ``aq``   — executed quantity
  * ``ai``   — matched order id created when the algo triggers
  * ``act``  — actual order type post-trigger (typically MARKET)

The previous implementation reused the ORDER_TRADE_UPDATE field codes
(``i`` / ``c`` / ``sp`` / ``z``), which silently produced empty
``order_id``/``client_order_id`` and zero ``trigger_price``/``filled_quantity``.
That made ``WebSocketManager._match_tp_level`` impossible — the SL never
moved on Binance after a TP fill.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.binance_adapter import BinanceAdapter  # noqa: E402


def _make_adapter() -> BinanceAdapter:
    exchange = MagicMock()
    exchange.options = {"enableDemoTrading": False}
    exchange.isSandboxModeEnabled = False
    exchange.urls = {
        "api": {"private": "https://fapi.binance.com", "public": "https://fapi.binance.com"}
    }
    exchange.markets = {"BTC/USDT:USDT": {"precision": {"amount": 3, "price": 1}}}
    return BinanceAdapter(
        ccxt_exchange=exchange,
        api_key="k",
        api_secret="s",
        rate_limiter=MagicMock(),
        mode="real",
    )


def test_algo_update_extracts_aid_as_order_id() -> None:
    """algoId from WS payload field ``aid`` must populate normalized order_id."""
    adapter = _make_adapter()
    payload = {
        "e": "ALGO_UPDATE",
        "E": 1700_000_000_000,
        "T": 1700_000_000_010,
        "o": {
            "aid": 123456789,  # algo id — primary identifier on Binance
            "caid": "client-algo-1",
            "at": "CONDITIONAL",
            "o": "TAKE_PROFIT_MARKET",
            "s": "BTCUSDT",
            "S": "SELL",
            "ps": "BOTH",
            "f": "GTC",
            "q": "0.1",
            "X": "FINISHED",
            "ai": 999_888,
            "ap": "70123.5",
            "aq": "0.1",
            "act": "MARKET",
            "tp": "70100",
            "p": "0",
        },
    }

    normalized = adapter._normalize_algo_update(payload)

    assert normalized is not None
    assert normalized["order_id"] == "123456789"
    assert normalized["client_order_id"] == "client-algo-1"


def test_algo_update_maps_tp_field_to_trigger_price() -> None:
    """``tp`` is the trigger price on Binance algo updates, not ``sp``."""
    adapter = _make_adapter()
    payload = {
        "e": "ALGO_UPDATE",
        "o": {
            "aid": 1,
            "caid": "x",
            "o": "STOP_MARKET",
            "X": "FINISHED",
            "tp": "68500",
            "q": "0.1",
            "aq": "0.1",
        },
    }

    normalized = adapter._normalize_algo_update(payload)

    assert normalized is not None
    assert normalized["trigger_price"] == pytest.approx(68500.0)
    # price falls back to trigger when no order price is set
    assert normalized["price"] == pytest.approx(68500.0)


def test_algo_update_maps_aq_to_filled_quantity() -> None:
    """Executed quantity sits in ``aq`` on algo updates, not ``z``."""
    adapter = _make_adapter()
    payload = {
        "e": "ALGO_UPDATE",
        "o": {
            "aid": 7,
            "caid": "y",
            "o": "TAKE_PROFIT_MARKET",
            "X": "FINISHED",
            "q": "0.5",
            "aq": "0.5",
            "tp": "100000",
        },
    }

    normalized = adapter._normalize_algo_update(payload)

    assert normalized is not None
    assert normalized["filled_quantity"] == pytest.approx(0.5)
    assert normalized["last_filled_quantity"] == pytest.approx(0.5)


def test_algo_update_status_finished_normalizes_to_distinct_finished() -> None:
    """``FINISHED`` must NOT alias to ``triggered`` — Binance emits both
    ``TRIGGERED`` and ``FINISHED`` for a single TP fill (algo condition
    met, then underlying market order completed). Aliasing them made the
    WS manager process the same fill twice and cascade onto the next
    open TP level. Keep ``finished`` as its own normalised state so
    ``_is_fill_event`` does NOT count it as a fill.
    """
    adapter = _make_adapter()
    payload = {
        "e": "ALGO_UPDATE",
        "o": {"aid": 1, "caid": "a", "o": "TAKE_PROFIT_MARKET", "X": "FINISHED"},
    }

    normalized = adapter._normalize_algo_update(payload)

    assert normalized is not None
    assert normalized["status"] == "finished"


def test_algo_update_canceled_status_does_not_appear_as_fill() -> None:
    adapter = _make_adapter()
    payload = {
        "e": "ALGO_UPDATE",
        "o": {"aid": 1, "caid": "a", "o": "STOP_MARKET", "X": "CANCELED"},
    }

    normalized = adapter._normalize_algo_update(payload)

    assert normalized is not None
    assert normalized["status"] == "cancelled"
    assert normalized["filled_quantity"] == 0


def test_algo_update_surfaces_matched_order_id_for_post_trigger_correlation() -> None:
    """``ai`` lets us correlate the algo with the regular order created post-trigger."""
    adapter = _make_adapter()
    payload = {
        "e": "ALGO_UPDATE",
        "o": {
            "aid": 1,
            "caid": "a",
            "o": "TAKE_PROFIT_MARKET",
            "X": "FINISHED",
            "ai": 555_777,
            "act": "MARKET",
        },
    }

    normalized = adapter._normalize_algo_update(payload)

    assert normalized is not None
    assert normalized["matched_order_id"] == "555777"
    assert normalized["actual_order_type"] == "MARKET"


def test_algo_update_with_legacy_ao_key_still_parses() -> None:
    """Some payload variants nest under ``ao`` instead of ``o``; both must work."""
    adapter = _make_adapter()
    payload = {
        "e": "ALGO_UPDATE",
        "ao": {
            "aid": 42,
            "caid": "ao-key",
            "o": "STOP_MARKET",
            "X": "FINISHED",
            "tp": "50000",
            "aq": "0.1",
        },
    }

    normalized = adapter._normalize_algo_update(payload)

    assert normalized is not None
    assert normalized["order_id"] == "42"
    assert normalized["trigger_price"] == pytest.approx(50000.0)


def test_order_trade_update_still_uses_canonical_order_field_codes() -> None:
    """ORDER_TRADE_UPDATE normalization must NOT regress."""
    adapter = _make_adapter()
    payload = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1700_000_000_000,
        "T": 1700_000_000_010,
        "o": {
            "i": 1234,
            "c": "client-1",
            "s": "BTCUSDT",
            "S": "SELL",
            "o": "MARKET",
            "ot": "STOP_MARKET",
            "x": "TRADE",
            "X": "FILLED",
            "sp": "68500",
            "ap": "68499.9",
            "L": "68499.9",
            "q": "0.1",
            "z": "0.1",
            "l": "0.1",
            "ps": "LONG",
            "R": True,
        },
    }

    normalized = adapter._normalize_order_trade_update(payload)

    assert normalized is not None
    assert normalized["order_id"] == "1234"
    assert normalized["client_order_id"] == "client-1"
    assert normalized["trigger_price"] == pytest.approx(68500.0)
    assert normalized["filled_quantity"] == pytest.approx(0.1)
    assert normalized["last_filled_quantity"] == pytest.approx(0.1)
    assert normalized["status"] == "filled"
    assert normalized["is_algo"] is False


def test_algo_update_provides_enough_for_match_tp_level() -> None:
    """Smoke: the normalized event for ``TRIGGERED`` (the actual fill
    notification) has every field WSManager._match_tp_level needs.

    With order_id populated, ``_match_tp_level`` matches via exchange_order_id.
    With trigger_price populated, the price-fallback branch also works.
    Both being zero/empty (the original bug) made the matcher fail
    silently and the SL never moved. ``FINISHED`` is intentionally not
    a fill event (see ``test_algo_update_status_finished_normalizes_to_distinct_finished``).
    """
    adapter = _make_adapter()
    payload = {
        "e": "ALGO_UPDATE",
        "o": {
            "aid": 100,
            "caid": "level-1-coid",
            "o": "TAKE_PROFIT_MARKET",
            "X": "TRIGGERED",
            "tp": "70100",
            "aq": "0.05",
        },
    }

    normalized = adapter._normalize_algo_update(payload)

    assert normalized is not None
    # Either of these is sufficient for _match_tp_level to find the level.
    assert normalized["order_id"] != ""
    assert normalized["trigger_price"] > 0
    # And the WSManager's _is_fill_event sees this as a fill.
    assert normalized["status"] == "triggered"
