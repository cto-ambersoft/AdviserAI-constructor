"""Unit tests for watcher event bus publishing, subscription, and routing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.services.watchers.event_bus as event_bus_mod  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.services.position.context import PositionContext, PositionSide  # noqa: E402
from app.services.watchers.event_bus import (  # noqa: E402
    WATCHER_EVENT_CHANNEL,
    handle_watcher_event,
    publish_watcher_event,
    subscribe_watcher_events,
)
from app.services.watchers.indicator_watcher import WatcherEvent  # noqa: E402


@pytest.fixture(autouse=True)
def _no_trigger_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    # I4 added a per-trigger Redis cooldown (ttl>0). Disable it (ttl=0) so the
    # routing tests are deterministic regardless of Redis. The dedup test re-enables
    # it with a fake Redis.
    monkeypatch.setattr(get_settings(), "watcher_trigger_cooldown_seconds", 0)


class _FakePubSub:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self._messages = messages
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def listen(self):
        for message in self._messages:
            yield message

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self, *, messages: list[dict[str, object]] | None = None) -> None:
        self.messages = messages or []
        self.published: list[tuple[str, str]] = []
        self.pubsub_instance = _FakePubSub(self.messages)

    async def __aenter__(self) -> _FakeRedis:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))

    def pubsub(self, *, ignore_subscribe_messages: bool = True) -> _FakePubSub:
        assert ignore_subscribe_messages is True
        return self.pubsub_instance


def _event(
    *,
    action: str = "tighten_sl",
    current_value: object = 123.4,
    action_params: dict[str, object] | None = None,
    market_price: float | None = None,
) -> WatcherEvent:
    return WatcherEvent(
        position_id="101",
        indicator="ATR",
        condition="> 1000",
        current_value=current_value,
        action=action,
        action_params=action_params or {"sl_offset_atr": 1.5},
        timestamp="2026-04-06T00:00:00+00:00",
        market_price=market_price,
    )


@pytest.mark.asyncio
async def test_publish_watcher_event_serializes_to_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "app.services.watchers.event_bus._get_redis_client",
        lambda: fake_redis,
    )

    event = _event(current_value={"line": 1.2, "signal": 0.8})
    await publish_watcher_event(event)

    assert fake_redis.published
    channel, payload = fake_redis.published[0]
    assert channel == WATCHER_EVENT_CHANNEL
    assert json.loads(payload) == {
        "position_id": "101",
        "indicator": "ATR",
        "condition": "> 1000",
        "current_value": {"line": 1.2, "signal": 0.8},
        "action": "tighten_sl",
        "action_params": {"sl_offset_atr": 1.5},
        "timestamp": "2026-04-06T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_subscribe_watcher_events_deserializes_and_invokes_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(action="alert", current_value=88.0)
    fake_redis = _FakeRedis(
        messages=[
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "position_id": event.position_id,
                        "indicator": event.indicator,
                        "condition": event.condition,
                        "current_value": event.current_value,
                        "action": event.action,
                        "action_params": event.action_params,
                        "timestamp": event.timestamp,
                    }
                ).encode("utf-8"),
            }
        ]
    )
    monkeypatch.setattr(
        "app.services.watchers.event_bus._get_redis_client",
        lambda: fake_redis,
    )
    handler = AsyncMock()

    await subscribe_watcher_events(handler)

    handler.assert_awaited_once()
    routed_event = handler.await_args.args[0]
    assert routed_event == event
    assert fake_redis.pubsub_instance.subscribed == [WATCHER_EVENT_CHANNEL]
    assert fake_redis.pubsub_instance.closed is True


@pytest.mark.asyncio
async def test_handle_watcher_event_tighten_sl_enqueues_replace_sl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = PositionContext(
        position_id="101",
        user_id="501",
        account_id="42",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        current_quantity=0.75,
        current_sl_price=98000.0,
        sl_exchange_order_id="sl-101",
        volatility_last_atr=1000.0,
    )
    queue = AsyncMock()
    queue.adapter = AsyncMock()
    queue.adapter.get_position.return_value = SimpleNamespace(mark_price=102000.0)

    monkeypatch.setattr(
        "app.services.watchers.event_bus.load_position_context", AsyncMock(return_value=position)
    )
    monkeypatch.setattr(
        "app.services.watchers.event_bus.get_order_queue", AsyncMock(return_value=queue)
    )

    await handle_watcher_event(_event(current_value=1000.0, action_params={"sl_offset_atr": 1.5}))

    queue.enqueue.assert_awaited_once()
    task = queue.enqueue.await_args.args[0]
    assert task.action == "replace_sl"
    assert task.params["symbol"] == "BTC/USDT:USDT"
    assert task.params["existing_order_id"] == "sl-101"
    assert task.params["new_quantity"] == pytest.approx(0.75)
    assert task.params["new_trigger_price"] == pytest.approx(100500.0)


@pytest.mark.asyncio
async def test_handle_watcher_event_tighten_sl_prefers_public_market_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = PositionContext(
        position_id="101",
        user_id="501",
        account_id="42",
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        current_quantity=0.75,
        current_sl_price=98000.0,
        sl_exchange_order_id="sl-101",
        volatility_last_atr=1000.0,
    )
    queue = AsyncMock()
    queue.adapter = AsyncMock()
    queue.adapter.get_position.return_value = SimpleNamespace(mark_price=102000.0)

    monkeypatch.setattr(
        "app.services.watchers.event_bus.load_position_context",
        AsyncMock(return_value=position),
    )
    monkeypatch.setattr(
        "app.services.watchers.event_bus.get_order_queue",
        AsyncMock(return_value=queue),
    )

    await handle_watcher_event(
        _event(
            current_value=1000.0,
            action_params={"sl_offset_atr": 1.5},
            market_price=101000.0,
        )
    )

    queue.enqueue.assert_awaited_once()
    task = queue.enqueue.await_args.args[0]
    assert task.params["new_trigger_price"] == pytest.approx(99500.0)


@pytest.mark.asyncio
async def test_handle_watcher_event_close_partial_enqueues_partial_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = PositionContext(
        position_id="101",
        user_id="501",
        account_id="42",
        symbol="BTC/USDT:USDT",
        side=PositionSide.SHORT,
        current_quantity=2.0,
    )
    queue = AsyncMock()

    monkeypatch.setattr(
        "app.services.watchers.event_bus.load_position_context", AsyncMock(return_value=position)
    )
    monkeypatch.setattr(
        "app.services.watchers.event_bus.get_order_queue", AsyncMock(return_value=queue)
    )

    await handle_watcher_event(
        _event(
            action="close_partial",
            current_value=55.0,
            action_params={"close_pct": 25},
        )
    )

    queue.enqueue.assert_awaited_once()
    task = queue.enqueue.await_args.args[0]
    assert task.action == "partial_close"
    assert task.params["quantity"] == pytest.approx(0.5)
    assert task.params["side"] == "buy"


@pytest.mark.asyncio
async def test_handle_watcher_event_alert_uses_notification_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = PositionContext(position_id="101", user_id="501", account_id="42")
    notification = AsyncMock()

    monkeypatch.setattr(
        "app.services.watchers.event_bus.load_position_context", AsyncMock(return_value=position)
    )
    monkeypatch.setattr("app.services.watchers.event_bus.send_watcher_notification", notification)

    await handle_watcher_event(_event(action="alert", current_value=77.0, action_params={}))

    notification.assert_awaited_once()


class _CooldownRedis:
    """SET NX EX semantics: first claim succeeds, repeats return None."""

    def __init__(self) -> None:
        self._keys: set[str] = set()

    async def __aenter__(self) -> _CooldownRedis:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> object:
        if nx and key in self._keys:
            return None
        self._keys.add(key)
        return True


async def test_claim_trigger_dedups_within_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    # I4: a persistent condition (same position/indicator/action) is claimed once;
    # repeats within the cooldown window are skipped.
    monkeypatch.setattr(get_settings(), "watcher_trigger_cooldown_seconds", 300)
    shared = _CooldownRedis()
    monkeypatch.setattr(event_bus_mod, "_get_redis_client", lambda: shared)

    evt = _event(action="tighten_sl")
    assert await event_bus_mod._claim_trigger(evt) is True
    assert await event_bus_mod._claim_trigger(evt) is False  # within cooldown
    # a different action on the same position is independent
    assert await event_bus_mod._claim_trigger(_event(action="close_partial")) is True


async def test_claim_trigger_fails_open_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "watcher_trigger_cooldown_seconds", 300)

    def _boom() -> object:
        raise ConnectionError("redis down")

    monkeypatch.setattr(event_bus_mod, "_get_redis_client", _boom)
    assert await event_bus_mod._claim_trigger(_event(action="tighten_sl")) is True
