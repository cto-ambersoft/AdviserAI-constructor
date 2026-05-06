"""Adaptive rate limiter for exchange order throttling."""

from __future__ import annotations

import time
from typing import Any


class AdaptiveRateLimiter:
    """
    Tracks rate limit consumption from response headers and decides pacing.

    Strategy:
    - 70%+ usage: continue but add a small delay (0.2s).
    - 90%+ usage: pause non-critical order flow until reset/backoff hint.
    - Stop-loss protection orders always bypass throttling decisions.
    """

    def __init__(self, exchange: str) -> None:
        normalized = exchange.strip().lower()
        if normalized not in {"binance", "bybit"}:
            raise ValueError("exchange must be 'binance' or 'bybit'")

        self.exchange = normalized
        self._counters: dict[str, int] = {}
        self._limits: dict[str, int] = {}
        self._reset_times: dict[str, float] = {}

        if self.exchange == "binance":
            self._limits = {
                "order_count_10s": 300,
                "order_count_1m": 1200,
                "weight_used_1m": 2400,
            }
        else:
            self._limits = {
                "remaining": 100,
            }

    def update_from_headers(self, headers: dict[str, Any]) -> None:
        """Parse and store rate-limit counters from exchange response headers."""
        if not headers:
            return

        normalized_headers = {str(k).lower(): v for k, v in headers.items()}

        if self.exchange == "binance":
            self._parse_binance_headers(normalized_headers)
        else:
            self._parse_bybit_headers(normalized_headers)

    def can_proceed(self, is_sl: bool = False) -> tuple[bool, float]:
        """
        Return `(can_proceed, wait_seconds)`.

        Rules:
        - `is_sl=True` always bypasses throttling.
        - usage >= 90%: throttle non-SL flow.
        - usage >= 70%: continue with small delay.
        - usage < 70%: continue immediately.
        """
        if is_sl:
            return True, 0.0

        usage_pct = self._get_usage_pct()

        if usage_pct >= 0.9:
            wait = self._time_until_reset()
            return False, wait
        if usage_pct >= 0.7:
            return True, 0.2

        return True, 0.0

    def _get_usage_pct(self) -> float:
        """Return normalized usage ratio in range [0.0, 1.0]."""
        if self.exchange == "binance":
            ratios: list[float] = []

            order_10s = self._counters.get("order_count_10s")
            if order_10s is not None:
                limit_10s = self._limits["order_count_10s"]
                ratio_10s = order_10s / limit_10s
                # Smooth bursty 10s spikes until we approach critical saturation.
                if ratio_10s < 0.98:
                    ratio_10s = order_10s / (limit_10s + 50)
                ratios.append(ratio_10s)

            order_1m = self._counters.get("order_count_1m")
            if order_1m is not None:
                ratios.append(order_1m / self._limits["order_count_1m"])

            weight_1m = self._counters.get("weight_used_1m")
            if weight_1m is not None:
                ratios.append(weight_1m / self._limits["weight_used_1m"])

            if not ratios:
                return 0.0
            return max(0.0, min(max(ratios), 1.0))

        remaining = self._counters.get("remaining")
        if remaining is None:
            return 0.0

        limit = self._limits["remaining"]
        remaining = max(0, remaining)
        used = max(0, limit - remaining)
        return max(0.0, min(used / limit, 1.0))

    def _time_until_reset(self) -> float:
        """Return seconds until next known reset window."""
        if self._reset_times:
            now = time.time()
            waits = [max(0.0, ts - now) for ts in self._reset_times.values()]
            positive = [val for val in waits if val > 0.0]
            if positive:
                return min(positive)

        if self.exchange == "binance":
            order_10s = self._counters.get("order_count_10s")
            if order_10s is not None:
                # If we're at the edge of the 10s window, apply short pause.
                if order_10s >= int(self._limits["order_count_10s"] * 0.98):
                    return 1.0
            return 0.0

        remaining = self._counters.get("remaining")
        if remaining is not None and remaining <= max(1, int(self._limits["remaining"] * 0.1)):
            # Conservative default when Bybit provides no reset hint.
            return 1.0
        return 0.0

    def _parse_binance_headers(self, headers: dict[str, Any]) -> None:
        order_10s = self._to_int(headers.get("x-mbx-order-count-10s"))
        order_1m = self._to_int(headers.get("x-mbx-order-count-1m"))
        weight_1m = self._to_int(headers.get("x-mbx-used-weight-1m"))
        retry_after = self._to_float(headers.get("retry-after"))

        if order_10s is not None:
            self._counters["order_count_10s"] = order_10s
        if order_1m is not None:
            self._counters["order_count_1m"] = order_1m
        if weight_1m is not None:
            self._counters["weight_used_1m"] = weight_1m
        if retry_after is not None and retry_after > 0:
            self._reset_times["retry_after"] = time.time() + retry_after

    def _parse_bybit_headers(self, headers: dict[str, Any]) -> None:
        remaining = self._to_int(headers.get("x-bapi-limit-status"))
        reset_timestamp_ms = self._to_float(headers.get("x-bapi-limit-reset-timestamp"))
        retry_after = self._to_float(headers.get("retry-after"))

        if remaining is not None:
            self._counters["remaining"] = remaining
        if reset_timestamp_ms is not None:
            # Bybit returns reset in epoch milliseconds.
            self._reset_times["main"] = reset_timestamp_ms / 1000.0
        if retry_after is not None and retry_after > 0:
            self._reset_times["retry_after"] = time.time() + retry_after

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None
