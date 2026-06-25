"""Regression: ``_validate_ccxt_environment`` must accept REAL ccxt clients.

The Binance adapter (and its Bybit mirror) previously walked the entire
``ccxt_exchange.urls`` tree looking for ``"testnet"`` / ``"binancefuture.com"``
substrings. That tree always contains a ``urls['test']`` sub-dict on every
fresh CCXT client — including production-only ones — so the heuristic
rejected ``mode='real'`` setups with::

    BinanceAdapter mode='real' cannot use a CCXT exchange configured for
    Binance sandbox endpoints.

The fix drops the URL-scan fallback and trusts the two authoritative
CCXT flags:

  * ``options['enableDemoTrading']`` (set by ``enable_demo_trading(True)``)
  * ``isSandboxModeEnabled``         (set by ``set_sandbox_mode(True)``)

These tests use **real** ``ccxt.async_support.binance`` /
``ccxt.async_support.bybit`` instances (no mocks) so the regression
cannot recur via test stubs that omit the pre-baked testnet sub-tree.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import ccxt.async_support as ccxt
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.binance_adapter import BinanceAdapter  # noqa: E402
from app.services.exchange.bybit_adapter import BybitAdapter  # noqa: E402

# ─────────────────────────── Binance ──────────────────────────────────────


def test_fresh_real_ccxt_binance_detects_as_mainnet() -> None:
    """Bug reproducer: a fresh ``ccxt.binance()`` client (no sandbox /
    demo configured) must be detected as ``mainnet``.

    Before the fix, the recursive URL scan picked up
    ``urls['test'].fapiPublic = 'https://testnet.binancefuture.com/...'``
    and returned ``"sandbox"``. ``mode='real'`` then rejected the client.
    """
    exchange = ccxt.binance({"apiKey": "k", "secret": "s", "options": {"defaultType": "future"}})
    try:
        # Sanity: the always-present sub-tree IS still there — that's the
        # whole point of the regression. We just must not let it lie to us.
        assert "test" in exchange.urls
        assert any(
            "testnet" in str(v).lower()
            for url_sub in exchange.urls.values()
            if isinstance(url_sub, dict)
            for v in url_sub.values()
        )

        assert BinanceAdapter._detect_ccxt_environment(exchange) == "mainnet"
        # End-to-end: ``mode='real'`` must accept the fresh client without raising.
        BinanceAdapter._validate_ccxt_environment(ccxt_exchange=exchange, mode="real")
    finally:
        # Synchronously discard the unused aiohttp session — ccxt.async_support
        # never opened it because we never made a request, but the destructor
        # would otherwise log a warning.
        exchange.options.clear()


def test_binance_demo_trading_detects_as_demo() -> None:
    exchange = ccxt.binance({"apiKey": "k", "secret": "s"})
    exchange.enable_demo_trading(True)
    try:
        assert BinanceAdapter._detect_ccxt_environment(exchange) == "demo"
        # ``mode='demo'`` accepts; ``mode='real'`` rejects.
        BinanceAdapter._validate_ccxt_environment(ccxt_exchange=exchange, mode="demo")
        with pytest.raises(ValueError, match="configured for Binance demo trading"):
            BinanceAdapter._validate_ccxt_environment(ccxt_exchange=exchange, mode="real")
    finally:
        exchange.options.clear()


def test_binance_set_sandbox_mode_detects_as_sandbox() -> None:
    """Explicit sandbox toggling via ``set_sandbox_mode(True)`` must still
    be detected — the validator must reject ``mode='real'`` here.
    """
    exchange = ccxt.binance({"apiKey": "k", "secret": "s"})
    exchange.set_sandbox_mode(True)
    try:
        assert BinanceAdapter._detect_ccxt_environment(exchange) == "sandbox"
        with pytest.raises(ValueError, match="sandbox endpoints"):
            BinanceAdapter._validate_ccxt_environment(ccxt_exchange=exchange, mode="real")
    finally:
        exchange.options.clear()


def test_binance_mock_exchange_with_minimal_urls_still_accepted_as_mainnet() -> None:
    """Adapter unit tests build a stripped-down MagicMock exchange. Confirm
    the new detector treats those as mainnet (preserving prior test
    behaviour now that the URL scan is gone)."""
    exchange = MagicMock()
    exchange.options = {"enableDemoTrading": False}
    exchange.isSandboxModeEnabled = False
    exchange.urls = {"api": {"private": "https://fapi.binance.com"}}

    assert BinanceAdapter._detect_ccxt_environment(exchange) == "mainnet"


# ─────────────────────────── Bybit ────────────────────────────────────────


def test_fresh_real_ccxt_bybit_detects_as_mainnet() -> None:
    """Same regression for Bybit: fresh ``ccxt.bybit()`` must be ``mainnet``.

    Before the fix, ``urls['test'].spot = 'https://api-testnet.{hostname}'``
    matched the ``"api-testnet."`` substring check and returned ``"sandbox"``.
    """
    exchange = ccxt.bybit({"apiKey": "k", "secret": "s", "options": {"defaultType": "swap"}})
    try:
        assert "test" in exchange.urls
        assert BybitAdapter._detect_ccxt_environment(exchange) == "mainnet"
        BybitAdapter._validate_ccxt_environment(ccxt_exchange=exchange, mode="real")
    finally:
        exchange.options.clear()


def test_bybit_set_sandbox_mode_detects_as_sandbox() -> None:
    exchange = ccxt.bybit({"apiKey": "k", "secret": "s"})
    exchange.set_sandbox_mode(True)
    try:
        assert BybitAdapter._detect_ccxt_environment(exchange) == "sandbox"
        with pytest.raises(ValueError, match="sandbox endpoints"):
            BybitAdapter._validate_ccxt_environment(ccxt_exchange=exchange, mode="real")
    finally:
        exchange.options.clear()


def test_bybit_demo_trading_detects_as_demo() -> None:
    exchange = ccxt.bybit({"apiKey": "k", "secret": "s"})
    # Bybit demo trading is exposed via ``enable_demo_trading`` in newer
    # CCXT builds. If unavailable on the installed version we skip — the
    # production validation still works via ``options['enableDemoTrading']``
    # which other tests cover.
    enable_demo = getattr(exchange, "enable_demo_trading", None)
    if not callable(enable_demo):
        pytest.skip("Installed CCXT build does not expose Bybit enable_demo_trading.")
    enable_demo(True)
    try:
        assert BybitAdapter._detect_ccxt_environment(exchange) == "demo"
    finally:
        exchange.options.clear()
