"""REST reconcile safety-net for real accounts whose user-data WS is silent.

Binance's user-data WebSocket can connect yet never deliver fill events on some
(real) accounts, so an SL/TP fill is unobserved and the position stays "open" in
the DB long after the exchange closed it. ``reconcile_open_positions_via_rest``
polls every open position and, once the exchange *confirms* the position is
flat, marks the DB row closed (idempotently). These tests pin that behavior and
— critically — that an open position is NOT closed, and a transient flat read is
re-confirmed before acting.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

import app.services.auto_trade.service as service_mod  # noqa: E402
from app.models.auto_trade_config import AutoTradeConfig  # noqa: E402
from app.models.auto_trade_event import AutoTradeEvent  # noqa: E402
from app.models.auto_trade_position import AutoTradePosition  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.exchange import ExchangeCredential  # noqa: E402
from app.models.personal_analysis_profile import PersonalAnalysisProfile  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.auto_trade.service import AutoTradeService  # noqa: E402
from app.services.exchange.adapter import (  # noqa: E402
    ConditionalOrderResult,
    PositionSnapshot,
)
from app.services.exchange.adapter import (  # noqa: E402
    PositionSide as AdapterPositionSide,
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reconcile.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_open_position(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        user = User(email="rec@example.com", hashed_password="x", is_active=True)
        s.add(user)
        await s.flush()
        profile = PersonalAnalysisProfile(
            user_id=user.id, symbol="BTCUSDT", query_prompt=None,
            agents={"x": True}, agent_weights={"x": 1.0}, interval_minutes=60,
            is_active=True, next_run_at=datetime.now(UTC),
            last_triggered_at=None, last_completed_at=None,
        )
        s.add(profile)
        await s.flush()
        acct = ExchangeCredential(
            user_id=user.id, exchange_name="binance", account_label="real",
            mode="real", encrypted_api_key="k", encrypted_api_secret="s",
            encrypted_passphrase=None,
        )
        s.add(acct)
        await s.flush()
        cfg = AutoTradeConfig(
            user_id=user.id, profile_id=profile.id, account_id=acct.id,
            enabled=True, is_running=True, position_size_usdt=100.0, leverage=1,
            min_confidence_pct=62.0, fast_close_confidence_pct=80.0,
            confirm_reports_required=2, risk_mode="1:2", sl_pct=1.0, tp_pct=2.0,
        )
        s.add(cfg)
        await s.flush()
        pos = AutoTradePosition(
            user_id=user.id, account_id=acct.id, config_id=cfg.id, profile_id=profile.id,
            symbol="BTC/USDT:USDT", side="SHORT", entry_price=63490.7,
            original_quantity=0.015, current_quantity=0.015, quantity=0.015,
            position_size_usdt=100.0, sl_price=64125.6, tp_price=62000.0,
            entry_confidence_pct=70.0, leverage=1, state="open", status="open",
            tp_mode="multi", tp_levels_json=[], sl_history_json=[], tp_history_json=[],
            active_watchers_json=[], adjustment_priority_json=[], transition_log_json=[],
            opened_at=datetime.now(UTC), sl_type="fixed", sl_exchange_order_id="sl-1",
        )
        s.add(pos)
        await s.commit()
        return pos.id


async def test_reconcile_marks_flat_position_closed(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pos_id = await _seed_open_position(db)
    monkeypatch.setattr(service_mod, "AsyncSessionFactory", db)

    service = AutoTradeService()
    # Exchange reports the position is gone (flat) — WS never told us.
    service._trading.fetch_futures_position = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.reconcile_open_positions_via_rest()

    assert result["checked"] == 1
    assert result["closed"] == 1
    async with db() as s:
        row = await s.get(AutoTradePosition, pos_id)
        assert row is not None
        assert row.state == "closed"
        assert row.status == "closed"
        assert row.close_reason == "reconciled_closed_on_exchange"
        assert float(row.current_quantity) == 0.0
        events = (await s.execute(select(AutoTradeEvent))).scalars().all()
        assert any(e.event_type == "position_reconciled_closed_via_rest" for e in events)


async def test_reconcile_leaves_open_position_open(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pos_id = await _seed_open_position(db)
    monkeypatch.setattr(service_mod, "AsyncSessionFactory", db)
    monkeypatch.setattr(service_mod.asyncio, "sleep", AsyncMock())  # skip confirm delays

    service = AutoTradeService()
    live = MagicMock()
    live.contracts = 0.015  # still open on the exchange
    live.mark_price = 63000.0
    service._trading.fetch_futures_position = AsyncMock(return_value=live)  # type: ignore[method-assign]

    result = await service.reconcile_open_positions_via_rest()

    assert result["checked"] == 1
    assert result["closed"] == 0
    async with db() as s:
        row = await s.get(AutoTradePosition, pos_id)
        assert row is not None
        assert row.state == "open"
        assert row.status == "open"


async def test_reconcile_is_idempotent_second_pass_noop(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_open_position(db)
    monkeypatch.setattr(service_mod, "AsyncSessionFactory", db)
    service = AutoTradeService()
    service._trading.fetch_futures_position = AsyncMock(return_value=None)  # type: ignore[method-assign]

    first = await service.reconcile_open_positions_via_rest()
    second = await service.reconcile_open_positions_via_rest()

    assert first["closed"] == 1
    # After the first pass the row is closed, so the second pass doesn't see it.
    assert second["checked"] == 0
    assert second["closed"] == 0


# ───────────────────── partial-TP SL reconcile (WS-gap) ──────────────────────
#
# On real accounts the user-data WS can connect yet never deliver fills, so an
# intermediate multi-TP rung fills on the exchange but the platform never runs
# the per-level SL shift (sl_lock_pct / move_sl_to). The REST reconciler now
# detects the fill — by the rung's algo order disappearing from openAlgoOrders,
# corroborated by the shrunken live size — and dispatches the same SL move the
# WS path would have. These tests pin that behavior.

# A long position: entry 62143, three TP rungs. TP1 (62609.2, close 25%,
# sl_lock_pct=-50 → "reduce risk") and TP2 (63075.2, close 50%, breakeven) carry
# SL directives; TP3 (63386.0, close 25%) is the final flattening rung. The
# original SL sits at 61520.7. After TP1 fills, sl_lock_pct=-50 puts the new SL
# at 62143 + (62609.2 - 62143) * -0.5 = 61909.9.
_ENTRY = 62143.0
_ORIG_QTY = 0.008
_ORIG_SL = 61520.7
_TP1_TRIGGER = 62609.2
_EXPECTED_TP1_SL = _ENTRY + (_TP1_TRIGGER - _ENTRY) * -0.5  # 61909.9


def _multi_tp_levels() -> list[dict]:
    return [
        {
            "level": 1, "price_offset_pct": 0.75, "close_pct": 25.0,
            "trigger_price": _TP1_TRIGGER, "status": "open",
            "exchange_order_id": "algo-tp1", "move_sl_to": None, "sl_lock_pct": -50.0,
        },
        {
            "level": 2, "price_offset_pct": 1.5, "close_pct": 50.0,
            "trigger_price": 63075.2, "status": "open",
            "exchange_order_id": "algo-tp2", "move_sl_to": None, "sl_lock_pct": 0.0,
        },
        {
            "level": 3, "price_offset_pct": 2.0, "close_pct": 25.0,
            "trigger_price": 63386.0, "status": "open",
            "exchange_order_id": "algo-tp3", "move_sl_to": None, "sl_lock_pct": None,
        },
    ]


async def _seed_multi_tp_position(
    factory: async_sessionmaker[AsyncSession], *, orig_qty: float = _ORIG_QTY
) -> int:
    async with factory() as s:
        user = User(email="mtp@example.com", hashed_password="x", is_active=True)
        s.add(user)
        await s.flush()
        profile = PersonalAnalysisProfile(
            user_id=user.id, symbol="BTCUSDT", query_prompt=None,
            agents={"x": True}, agent_weights={"x": 1.0}, interval_minutes=60,
            is_active=True, next_run_at=datetime.now(UTC),
            last_triggered_at=None, last_completed_at=None,
        )
        s.add(profile)
        await s.flush()
        acct = ExchangeCredential(
            user_id=user.id, exchange_name="binance", account_label="real",
            mode="real", encrypted_api_key="k", encrypted_api_secret="s",
            encrypted_passphrase=None,
        )
        s.add(acct)
        await s.flush()
        cfg = AutoTradeConfig(
            user_id=user.id, profile_id=profile.id, account_id=acct.id,
            enabled=True, is_running=True, position_size_usdt=100.0, leverage=1,
            min_confidence_pct=62.0, fast_close_confidence_pct=80.0,
            confirm_reports_required=2, risk_mode="1:2", sl_pct=1.0, tp_pct=2.0,
        )
        s.add(cfg)
        await s.flush()
        pos = AutoTradePosition(
            user_id=user.id, account_id=acct.id, config_id=cfg.id, profile_id=profile.id,
            symbol="BTC/USDT:USDT", side="LONG", entry_price=_ENTRY,
            original_quantity=orig_qty, current_quantity=orig_qty, quantity=orig_qty,
            position_size_usdt=100.0, sl_price=_ORIG_SL, tp_price=_TP1_TRIGGER,
            entry_confidence_pct=70.0, leverage=1, state="open", status="open",
            tp_mode="multi", tp_levels_json=_multi_tp_levels(), sl_history_json=[],
            tp_history_json=[], active_watchers_json=[], adjustment_priority_json=[],
            transition_log_json=[], opened_at=datetime.now(UTC), sl_type="fixed",
            sl_exchange_order_id="sl-orig",
        )
        s.add(pos)
        await s.commit()
        return pos.id


class _FakeAdapter:
    """Adapter exposing only what the partial-TP reconcile + engine touch."""

    def __init__(self, *, open_algo_ids: list[str], live_size: float) -> None:
        self._open_algo_ids = open_algo_ids
        self._live_size = live_size

    async def get_open_conditional_orders(self, symbol: str) -> list[ConditionalOrderResult]:
        return [
            ConditionalOrderResult(
                exchange_order_id=algo_id, client_order_id="", order_type="take_profit",
                trigger_price=0.0, quantity=0.0, status="new", is_algo=True,
            )
            for algo_id in self._open_algo_ids
        ]

    async def get_position(self, symbol: str) -> PositionSnapshot:
        return PositionSnapshot(
            symbol=symbol, side=AdapterPositionSide.LONG, size=self._live_size,
            entry_price=_ENTRY, unrealized_pnl=0.0, leverage=1, mark_price=62650.0,
            liquidation_price=0.0, open_orders=[],
        )


class _FakeQueue:
    def __init__(self) -> None:
        self.tasks: list = []
        # Records the ``session`` kwarg each get_order_queue call received. The
        # deadlock fix requires the queue to be fetched with NO open DB session
        # (i.e. outside the FOR UPDATE transaction); a non-None session here
        # means the AB-BA regression is back.
        self.get_order_queue_sessions: list = []

    async def enqueue(self, task) -> None:
        self.tasks.append(task)


def _wire_partial_tp_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_algo_ids: list[str],
    live_size: float,
) -> tuple[_FakeQueue, MagicMock]:
    """Patch adapter + order-queue factories and the not-flat live read."""
    adapter = _FakeAdapter(open_algo_ids=open_algo_ids, live_size=live_size)
    queue = _FakeQueue()

    async def _fake_adapter(position, *, session=None):
        return adapter

    async def _fake_queue(position, *, session=None):
        queue.get_order_queue_sessions.append(session)
        return queue

    monkeypatch.setattr(service_mod, "create_exchange_adapter_for_position", _fake_adapter)
    monkeypatch.setattr(service_mod, "get_order_queue", _fake_queue)
    monkeypatch.setattr(service_mod.asyncio, "sleep", AsyncMock())

    live = MagicMock()
    live.contracts = live_size  # still open on the exchange, but shrunk
    live.mark_price = 62650.0
    return queue, live


async def test_partial_tp_moves_sl_via_rest_when_ws_silent(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pos_id = await _seed_multi_tp_position(db)
    monkeypatch.setattr(service_mod, "AsyncSessionFactory", db)

    # TP1's algo order is gone from openAlgoOrders → it filled; position shrank
    # from 0.008 to 0.006 (25% closed). TP2/TP3 are still resting.
    queue, live = _wire_partial_tp_mocks(
        monkeypatch, open_algo_ids=["algo-tp2", "algo-tp3"], live_size=0.006
    )

    service = AutoTradeService()
    service._trading.fetch_futures_position = AsyncMock(return_value=live)  # type: ignore[method-assign]

    result = await service.reconcile_open_positions_via_rest()

    # Position is still open (partial), so it is NOT counted as closed.
    assert result["checked"] == 1
    assert result["closed"] == 0

    # TP1 advanced; an SL replace was dispatched at the reduce-risk price.
    replace_tasks = [t for t in queue.tasks if t.action == "replace_sl"]
    assert len(replace_tasks) == 1
    assert replace_tasks[0].params["new_trigger_price"] == pytest.approx(_EXPECTED_TP1_SL)

    async with db() as s:
        row = await s.get(AutoTradePosition, pos_id)
        assert row is not None
        assert row.status == "open"  # still open — only an intermediate rung filled
        assert float(row.current_quantity) == pytest.approx(0.006)
        assert row.tp_levels_json[0]["status"] == "triggered"
        assert row.tp_levels_json[1]["status"] == "open"
        events = (await s.execute(select(AutoTradeEvent))).scalars().all()
        assert any(e.event_type == "multi_tp_reconciled_via_rest" for e in events)

    # The SL-replace on_success callback persists the new SL price + history.
    await replace_tasks[0].on_success(
        ConditionalOrderResult(
            exchange_order_id="sl-moved", client_order_id="", order_type="stop_loss",
            trigger_price=_EXPECTED_TP1_SL, quantity=0.006, status="new", is_algo=True,
        )
    )
    async with db() as s:
        row = await s.get(AutoTradePosition, pos_id)
        assert row is not None
        assert float(row.sl_price) == pytest.approx(_EXPECTED_TP1_SL)
        assert row.sl_exchange_order_id == "sl-moved"
        assert len(row.sl_history_json) == 1
        assert row.sl_history_json[0]["trigger_source"] == "rest_reconcile_inferred"


async def test_partial_tp_skips_when_live_size_inconsistent(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pos_id = await _seed_multi_tp_position(db)
    monkeypatch.setattr(service_mod, "AsyncSessionFactory", db)

    # TP1's algo order is gone, but the live size is still the full 0.008 — the
    # order was cancelled, not filled. The reconciler must refuse to move the SL.
    queue, live = _wire_partial_tp_mocks(
        monkeypatch, open_algo_ids=["algo-tp2", "algo-tp3"], live_size=0.008
    )

    service = AutoTradeService()
    service._trading.fetch_futures_position = AsyncMock(return_value=live)  # type: ignore[method-assign]

    await service.reconcile_open_positions_via_rest()

    assert not [t for t in queue.tasks if t.action == "replace_sl"]
    async with db() as s:
        row = await s.get(AutoTradePosition, pos_id)
        assert row is not None
        assert row.tp_levels_json[0]["status"] == "open"  # untouched
        assert float(row.sl_price) == pytest.approx(_ORIG_SL)
        events = (await s.execute(select(AutoTradeEvent))).scalars().all()
        assert any(e.event_type == "multi_tp_rest_reconcile_size_mismatch" for e in events)


async def test_partial_tp_noop_when_all_rungs_still_resting(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pos_id = await _seed_multi_tp_position(db)
    monkeypatch.setattr(service_mod, "AsyncSessionFactory", db)

    # All three algo orders are still open → nothing filled; no SL move, no event.
    queue, live = _wire_partial_tp_mocks(
        monkeypatch, open_algo_ids=["algo-tp1", "algo-tp2", "algo-tp3"], live_size=0.008
    )

    service = AutoTradeService()
    service._trading.fetch_futures_position = AsyncMock(return_value=live)  # type: ignore[method-assign]

    await service.reconcile_open_positions_via_rest()

    assert not queue.tasks
    async with db() as s:
        row = await s.get(AutoTradePosition, pos_id)
        assert row is not None
        assert row.tp_levels_json[0]["status"] == "open"
        events = (await s.execute(select(AutoTradeEvent))).scalars().all()
        assert not [e for e in events if e.event_type.startswith("multi_tp")]


async def test_partial_tp_rounded_fills_move_sl_not_mismatch(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for prod position 274: LOT_SIZE rounding must not trip the guard.

    original=0.007, TP1 25% (nominal 0.00175 → step-rounded 0.001) and TP2 50%
    (nominal 0.0035 → 0.003) both fill → live=0.003 (0.004 closed). The old guard
    compared live against the *un-rounded* expected remaining (0.00175) and
    looped ``size_mismatch`` forever, never moving the SL. The rounding-aware
    guard must instead confirm both rungs and shift the SL to TP2 breakeven.
    """
    pos_id = await _seed_multi_tp_position(db, orig_qty=0.007)
    monkeypatch.setattr(service_mod, "AsyncSessionFactory", db)

    # TP1 + TP2 algos gone (filled); only TP3 still resting. Live shrank 0.007→0.003.
    queue, live = _wire_partial_tp_mocks(
        monkeypatch, open_algo_ids=["algo-tp3"], live_size=0.003
    )

    service = AutoTradeService()
    service._trading.fetch_futures_position = AsyncMock(return_value=live)  # type: ignore[method-assign]

    await service.reconcile_open_positions_via_rest()

    # No mismatch event; the SL advanced to TP2's breakeven (sl_lock_pct=0 → entry).
    replace_tasks = [t for t in queue.tasks if t.action == "replace_sl"]
    assert replace_tasks, "expected at least one replace_sl (SL should have moved)"
    assert replace_tasks[-1].params["new_trigger_price"] == pytest.approx(_ENTRY)
    # Deadlock regression: the order queue must be fetched OUTSIDE the FOR UPDATE
    # transaction (no session threaded in), else the AB-BA freeze returns.
    assert queue.get_order_queue_sessions == [None]
    async with db() as s:
        row = await s.get(AutoTradePosition, pos_id)
        assert row is not None
        assert row.tp_levels_json[0]["status"] == "triggered"
        assert row.tp_levels_json[1]["status"] == "triggered"
        assert row.tp_levels_json[2]["status"] == "open"
        assert float(row.current_quantity) == pytest.approx(0.003)
        events = (await s.execute(select(AutoTradeEvent))).scalars().all()
        assert not [e for e in events if e.event_type == "multi_tp_rest_reconcile_size_mismatch"]
        assert any(e.event_type == "multi_tp_reconciled_via_rest" for e in events)
