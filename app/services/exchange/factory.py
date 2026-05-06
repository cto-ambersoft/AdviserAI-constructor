"""Factory for creating concrete exchange adapters."""

from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt

from app.schemas.exchange_trading import ExchangeMode
from app.services.exchange.adapter import ExchangeAdapter
from app.services.exchange.binance_adapter import BinanceAdapter
from app.services.exchange.bybit_adapter import BybitAdapter
from app.services.exchange.rate_limiter import AdaptiveRateLimiter


class ExchangeAdapterFactory:
    """Creates an exchange-specific adapter instance."""

    @staticmethod
    async def create(
        exchange_name: str,
        api_key: str,
        api_secret: str,
        mode: ExchangeMode = "real",
    ) -> ExchangeAdapter:
        normalized = exchange_name.strip().lower()

        if normalized == "binance":
            ccxt_exchange = ccxt.binance(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,
                    "options": {"defaultType": "future"},
                }
            )
            if mode == "demo":
                ExchangeAdapterFactory._enable_demo_mode(
                    ccxt_exchange,
                    exchange_name=normalized,
                )
            return BinanceAdapter(
                ccxt_exchange=ccxt_exchange,
                api_key=api_key,
                api_secret=api_secret,
                rate_limiter=AdaptiveRateLimiter("binance"),
                mode=mode,
            )

        if normalized == "bybit":
            ccxt_exchange = ccxt.bybit(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,
                    "options": {"defaultType": "swap"},
                }
            )
            if mode == "demo":
                ExchangeAdapterFactory._enable_demo_mode(
                    ccxt_exchange,
                    exchange_name=normalized,
                )
            return BybitAdapter(
                ccxt_exchange=ccxt_exchange,
                api_key=api_key,
                api_secret=api_secret,
                rate_limiter=AdaptiveRateLimiter("bybit"),
                mode=mode,
            )

        raise ValueError(f"Unsupported exchange: {exchange_name}")

    @staticmethod
    def _enable_demo_mode(exchange: Any, *, exchange_name: str) -> None:
        enable_demo = getattr(exchange, "enable_demo_trading", None)
        if not callable(enable_demo):
            raise RuntimeError(
                f"{exchange_name} demo trading is unavailable in the configured CCXT client."
            )
        try:
            enable_demo(True)
        except Exception as exc:
            raise RuntimeError(f"Failed to enable {exchange_name} demo trading.") from exc
