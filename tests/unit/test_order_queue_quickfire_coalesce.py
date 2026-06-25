"""Regression: quick-fire ``replace_sl`` tasks are coalesced.

Defence-in-depth against the production cascade: even if a future code
path bypasses ``MultiTPEngine``'s dedup-set guard, the queue itself
collapses multi-TP ``replace_sl`` tasks for the same position arriving
within a 0.5s window into a single latest-intent task.

Trailing/breakeven/volatility sources carry distinct ``reason`` prefixes
(``realtime_pipeline:...``) and MUST NOT be coalesced into a multi-TP
move (or vice versa).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.exchange.adapter import ExchangeAdapter, OrderSide  # noqa: E402
from app.services.position.order_queue import (  # noqa: E402
    OrderExecutionQueue,
    OrderPriority,
    OrderTask,
    set_safety_audit_hook,
)


def _replace_sl_task(*, target: float, reason: str, created_at: float) -> OrderTask:
    return OrderTask(
        priority=OrderPriority.SL_ADJUSTMENT,
        created_at=created_at,
        position_id="pos-quickfire",
        action="replace_sl",
        params={
            "symbol": "BTC/USDT:USDT",
            "side": OrderSide.SELL,
            "existing_order_id": "sl-1",
            "new_trigger_price": target,
            "trigger_price": target,
            "new_quantity": 1.0,
            "full_quantity": 1.0,
            "client_order_id": f"coid-{target}",
            "close_position": True,
            "reason": reason,
        },
    )


async def test_two_multi_tp_replace_sl_within_window_coalesce_to_one() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")

    audits: list[tuple[str, dict[str, Any]]] = []

    async def _hook(event_type: str, payload: dict[str, Any]) -> None:
        audits.append((event_type, payload))

    set_safety_audit_hook(_hook)
    try:
        now = time.time()
        await queue.enqueue(
            _replace_sl_task(
                target=100_500.0,
                reason="tp1_hit_sl_adjustment",
                created_at=now,
            )
        )
        await queue.enqueue(
            _replace_sl_task(
                target=101_000.0,
                reason="tp2_hit_sl_adjustment",
                created_at=now + 0.1,  # within 0.5s window
            )
        )
    finally:
        set_safety_audit_hook(None)

    # One task in the pending map (the latest intent — target 101_000).
    assert len(queue._pending_tasks) == 1  # type: ignore[attr-defined]
    coalesced = list(queue._pending_tasks.values())[0]  # type: ignore[attr-defined]
    assert coalesced.params["new_trigger_price"] == pytest.approx(101_000.0)
    assert coalesced.params["reason"] == "tp2_hit_sl_adjustment"

    coalesce_audits = [event for event in audits if event[0] == "replace_sl_coalesced_inflight"]
    assert len(coalesce_audits) == 1
    assert coalesce_audits[0][1]["new_target"] == pytest.approx(101_000.0)
    assert coalesce_audits[0][1]["previous_target"] == pytest.approx(100_500.0)


async def test_trailing_and_multi_tp_do_not_coalesce() -> None:
    """A trailing-source replace_sl and a multi-TP-source replace_sl must
    remain separate. Coalescing across sources would let trailing silently
    override a multi-TP lock-in or vice versa."""
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")

    now = time.time()
    await queue.enqueue(
        _replace_sl_task(
            target=100_500.0,
            reason="tp1_hit_sl_adjustment",
            created_at=now,
        )
    )
    await queue.enqueue(
        _replace_sl_task(
            target=99_800.0,
            reason="realtime_pipeline:trailing",
            created_at=now + 0.05,
        )
    )

    # Two distinct tasks — different price keys, different sources.
    assert len(queue._pending_tasks) == 2  # type: ignore[attr-defined]


async def test_replace_sl_outside_window_does_not_coalesce() -> None:
    adapter = AsyncMock(spec=ExchangeAdapter)
    queue = OrderExecutionQueue(adapter=adapter, account_id="acc-1")

    now = time.time()
    await queue.enqueue(
        _replace_sl_task(
            target=100_500.0,
            reason="tp1_hit_sl_adjustment",
            created_at=now,
        )
    )
    await queue.enqueue(
        _replace_sl_task(
            target=101_000.0,
            reason="tp2_hit_sl_adjustment",
            created_at=now + 1.0,  # well outside 0.5s window
        )
    )

    # Both kept (distinct price keys, no quickfire coalesce).
    assert len(queue._pending_tasks) == 2  # type: ignore[attr-defined]
