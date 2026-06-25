"""T7 (W5b): the in-position indicator watcher publishes RSI/MACD/EMA-cross triggers
to Redis, but nothing consumed them in production — the runtime never started a
subscriber. These tests pin that the runtime now starts a resilient consumer that
routes events to ``handle_watcher_event``.
"""

import asyncio
import contextlib

import pytest

import app.services.auto_trade.service as svc
import app.services.watchers.event_bus as eb


async def test_consumer_loop_subscribes_with_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_subscribe(handler: object) -> None:
        captured["handler"] = handler
        raise asyncio.CancelledError

    monkeypatch.setattr(eb, "subscribe_watcher_events", fake_subscribe)
    with pytest.raises(asyncio.CancelledError):
        await svc._watcher_event_consumer_loop()
    assert captured["handler"] is eb.handle_watcher_event


async def test_consumer_loop_retries_after_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_subscribe(handler: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("redis down")  # transient: must not kill the loop
        raise asyncio.CancelledError

    monkeypatch.setattr(eb, "subscribe_watcher_events", fake_subscribe)
    monkeypatch.setattr(svc, "_WATCHER_RESUBSCRIBE_DELAY_SECONDS", 0.0)
    with pytest.raises(asyncio.CancelledError):
        await svc._watcher_event_consumer_loop()
    assert len(calls) == 2  # retried after the transient error


async def test_install_runtime_starts_watcher_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def fake_subscribe(handler: object) -> None:
        started.set()
        await asyncio.sleep(3600)

    async def fake_reconciler(service: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(eb, "subscribe_watcher_events", fake_subscribe)
    monkeypatch.setattr(svc, "_reconciler_loop", fake_reconciler)

    # Neutralize the process-global hook setters so the test doesn't leak state.
    import app.services.audit as audit_mod
    from app.services.position import order_queue as oq_mod

    monkeypatch.setattr(audit_mod, "set_audit_hook", lambda *_a, **_k: None)
    monkeypatch.setattr(oq_mod, "set_fatal_error_audit_hook", lambda *_a, **_k: None)
    monkeypatch.setattr(oq_mod, "set_safety_audit_hook", lambda *_a, **_k: None)

    service = svc.AutoTradeService()

    async def _noop() -> None:
        return None

    monkeypatch.setattr(service, "hydrate_active_positions", _noop)

    task = await svc.install_auto_trade_runtime(service)
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    assert started.is_set()
