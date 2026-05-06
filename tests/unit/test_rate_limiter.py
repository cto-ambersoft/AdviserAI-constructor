"""Unit tests for adaptive exchange rate limiter."""

from __future__ import annotations

from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.rate_limiter import AdaptiveRateLimiter  # noqa: E402


def test_binance_soft_zone_returns_small_delay() -> None:
    limiter = AdaptiveRateLimiter("binance")
    limiter.update_from_headers({"X-MBX-ORDER-COUNT-10S": "280"})

    can_proceed, wait = limiter.can_proceed()

    assert can_proceed is True
    assert wait == pytest.approx(0.2)


def test_binance_hard_zone_and_sl_bypass() -> None:
    limiter = AdaptiveRateLimiter("binance")
    limiter.update_from_headers({"X-MBX-ORDER-COUNT-10S": "295"})

    can_proceed, wait = limiter.can_proceed(is_sl=False)
    sl_can_proceed, sl_wait = limiter.can_proceed(is_sl=True)

    assert can_proceed is False
    assert wait > 0.0
    assert sl_can_proceed is True
    assert sl_wait == pytest.approx(0.0)


def test_bybit_remaining_header_ok_then_throttle() -> None:
    limiter = AdaptiveRateLimiter("bybit")
    limiter.update_from_headers({"X-Bapi-Limit-Status": "50"})

    can_proceed_ok, wait_ok = limiter.can_proceed()
    assert can_proceed_ok is True
    assert wait_ok == pytest.approx(0.0)

    limiter.update_from_headers({"X-Bapi-Limit-Status": "5"})
    can_proceed_throttle, wait_throttle = limiter.can_proceed()
    assert can_proceed_throttle is False
    assert wait_throttle > 0.0


def test_empty_state_is_optimistic_default() -> None:
    limiter = AdaptiveRateLimiter("binance")
    can_proceed, wait = limiter.can_proceed()

    assert can_proceed is True
    assert wait == pytest.approx(0.0)


def test_reset_time_calculation_returns_around_three_seconds() -> None:
    limiter = AdaptiveRateLimiter("bybit")
    reset_ts_ms = int((time.time() + 3.0) * 1000)
    limiter.update_from_headers(
        {
            "X-Bapi-Limit-Status": "5",
            "X-Bapi-Limit-Reset-Timestamp": str(reset_ts_ms),
        }
    )

    wait = limiter._time_until_reset()
    assert 2.5 <= wait <= 3.2
