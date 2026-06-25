"""Bug W: a cached WebSocketManager whose user-data stream died must be
restarted by the hydrate/track path, not silently reused.

Before the fix, ``_ensure_ws_manager_tracked`` called ``manager.start()`` only
when *creating* a new manager; an existing (cached) manager was only
``track_position``-ed. So once an account's user-data stream went silent, the
60s hydrate loop kept tracking positions onto a dead connection forever and no
TP/SL fills were ever observed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.services.auto_trade.service as service_mod  # noqa: E402
from app.services.auto_trade.service import AutoTradeService  # noqa: E402
from app.services.position.context import PositionContext  # noqa: E402
from app.services.position.state_machine import PositionState  # noqa: E402


def _ctx(account_id: str, position_id: str) -> PositionContext:
    ctx = PositionContext(symbol="BTC/USDT:USDT")
    ctx.account_id = account_id
    ctx.user_id = "1"
    ctx.position_id = position_id
    # Non-OPEN so the realtime-SL pipeline branch is skipped in the test.
    ctx.state = PositionState.ENTERING
    return ctx


def _fake_manager(*, connected: bool, reconnecting: bool) -> MagicMock:
    m = MagicMock()
    m.is_connected.return_value = connected
    m.is_reconnecting.return_value = reconnecting
    m.start = AsyncMock()
    return m


async def test_dead_cached_manager_is_restarted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_manager(connected=False, reconnecting=False)
    monkeypatch.setitem(service_mod._WS_MANAGER_REGISTRY, "17", fake)

    service = AutoTradeService()
    await service._ensure_ws_manager_tracked(session=None, position=_ctx("17", "228"))

    fake.start.assert_awaited_once()
    fake.track_position.assert_called_once()


async def test_connected_cached_manager_not_restarted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_manager(connected=True, reconnecting=False)
    monkeypatch.setitem(service_mod._WS_MANAGER_REGISTRY, "16", fake)

    service = AutoTradeService()
    await service._ensure_ws_manager_tracked(session=None, position=_ctx("16", "229"))

    fake.start.assert_not_awaited()
    fake.track_position.assert_called_once()


async def test_reconnecting_cached_manager_not_double_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Disconnected but a reconnect cycle is already running — must NOT start again.
    fake = _fake_manager(connected=False, reconnecting=True)
    monkeypatch.setitem(service_mod._WS_MANAGER_REGISTRY, "18", fake)

    service = AutoTradeService()
    await service._ensure_ws_manager_tracked(session=None, position=_ctx("18", "230"))

    fake.start.assert_not_awaited()
    fake.track_position.assert_called_once()
