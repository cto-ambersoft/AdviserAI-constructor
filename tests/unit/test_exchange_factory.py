"""Unit tests for exchange adapter factory."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.binance_adapter import BinanceAdapter  # noqa: E402
from app.services.exchange.bybit_adapter import BybitAdapter  # noqa: E402
from app.services.exchange.factory import ExchangeAdapterFactory  # noqa: E402


class _DummyExchange:
    def __init__(self) -> None:
        self.demo_enabled = False
        self.sandbox_enabled = False
        self.options: dict[str, Any] = {"enableDemoTrading": False}
        self.isSandboxModeEnabled = False
        self.urls: dict[str, Any] = {
            "api": {
                "private": "https://api.bybit.com",
                "public": "https://api.bybit.com",
            }
        }

    def enable_demo_trading(self, enabled: bool) -> None:
        self.demo_enabled = enabled
        self.options["enableDemoTrading"] = enabled
        if enabled:
            self.urls["api"] = {
                "private": "https://api-demo.bybit.com",
                "public": "https://api-demo.bybit.com",
            }

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox_enabled = enabled
        self.isSandboxModeEnabled = enabled
        if enabled:
            self.urls["api"] = {
                "private": "https://api-testnet.bybit.com",
                "public": "https://api-testnet.bybit.com",
            }


async def test_factory_creates_binance_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_config: dict[str, Any] = {}
    exchange = _DummyExchange()

    def _binance_factory(config: dict[str, Any]) -> _DummyExchange:
        captured_config.update(config)
        return exchange

    monkeypatch.setattr("app.services.exchange.factory.ccxt.binance", _binance_factory)

    adapter = await ExchangeAdapterFactory.create(
        exchange_name="binance",
        api_key="key-1",
        api_secret="secret-1",
        mode="real",
    )

    assert isinstance(adapter, BinanceAdapter)
    assert captured_config["apiKey"] == "key-1"
    assert captured_config["secret"] == "secret-1"
    assert captured_config["options"]["defaultType"] == "future"
    assert exchange.sandbox_enabled is False


async def test_factory_creates_binance_adapter_in_demo_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_config: dict[str, Any] = {}
    exchange = _DummyExchange()

    def _binance_factory(config: dict[str, Any]) -> _DummyExchange:
        captured_config.update(config)
        return exchange

    monkeypatch.setattr("app.services.exchange.factory.ccxt.binance", _binance_factory)

    adapter = await ExchangeAdapterFactory.create(
        exchange_name="binance",
        api_key="key-1",
        api_secret="secret-1",
        mode="demo",
    )

    assert isinstance(adapter, BinanceAdapter)
    assert captured_config["apiKey"] == "key-1"
    assert captured_config["secret"] == "secret-1"
    assert captured_config["options"]["defaultType"] == "future"
    assert exchange.demo_enabled is True
    assert exchange.sandbox_enabled is False
    assert adapter._base_url == "https://demo-fapi.binance.com"


async def test_factory_creates_bybit_adapter_in_demo_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_config: dict[str, Any] = {}
    exchange = _DummyExchange()

    def _bybit_factory(config: dict[str, Any]) -> _DummyExchange:
        captured_config.update(config)
        return exchange

    monkeypatch.setattr("app.services.exchange.factory.ccxt.bybit", _bybit_factory)

    adapter = await ExchangeAdapterFactory.create(
        exchange_name="bybit",
        api_key="key-2",
        api_secret="secret-2",
        mode="demo",
    )

    assert isinstance(adapter, BybitAdapter)
    assert captured_config["apiKey"] == "key-2"
    assert captured_config["secret"] == "secret-2"
    assert captured_config["options"]["defaultType"] == "swap"
    assert exchange.demo_enabled is True
    assert exchange.sandbox_enabled is False
    assert adapter._base_url == "https://api-demo.bybit.com"
    assert adapter._PRIVATE_WS_URL == "wss://stream-demo.bybit.com/v5/private"
    assert adapter._PUBLIC_LINEAR_WS_URL == "wss://stream.bybit.com/v5/public/linear"


async def test_factory_raises_when_demo_mode_cannot_be_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenDemoExchange(_DummyExchange):
        def enable_demo_trading(self, enabled: bool) -> None:
            raise RuntimeError("demo unavailable")

    exchange = _BrokenDemoExchange()

    monkeypatch.setattr(
        "app.services.exchange.factory.ccxt.binance",
        lambda _config: exchange,
    )

    with pytest.raises(RuntimeError, match="Failed to enable binance demo trading"):
        await ExchangeAdapterFactory.create(
            exchange_name="binance",
            api_key="key-1",
            api_secret="secret-1",
            mode="demo",
        )


async def test_factory_raises_for_unknown_exchange() -> None:
    with pytest.raises(ValueError, match="Unsupported exchange"):
        await ExchangeAdapterFactory.create(
            exchange_name="kraken",
            api_key="key",
            api_secret="secret",
        )
