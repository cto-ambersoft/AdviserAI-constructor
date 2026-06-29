"""S2: Outcome-Aware shadow outcomes — as-of correctness, idempotent record, backfill."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.oa_shadow_outcome import OaShadowOutcome
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.services.market_data.service import MarketDataService
from app.services.oa_shadow import (
    OaShadowService,
    compute_realized_move_pct,
    extract_prediction,
)

_NOW = datetime(2026, 6, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oa_shadow.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _frame(candles: list[dict[str, Any]]) -> Any:
    return MarketDataService.frame_from_candles(candles)


class _FakeMarketData:
    """Returns a fixed OHLCV frame; records the fetch_ohlcv kwargs."""

    def __init__(self, frame: Any) -> None:
        self._frame = frame
        self.calls: list[dict[str, Any]] = []

    async def fetch_ohlcv(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._frame


async def _history(
    session: AsyncSession,
    *,
    completed_at: datetime,
    history_id: int,
    analysis_data: dict[str, object] | None = None,
) -> PersonalAnalysisHistory:
    row = PersonalAnalysisHistory(
        id=history_id,
        user_id=1,
        profile_id=1,
        trade_job_id=f"job-{history_id}",
        symbol="BTCUSDT",
        analysis_data=analysis_data
        or {"aiTrend": {"direction": "up", "strength": 0.7}, "decisionEventId": "evt-x"},
        core_completed_at=completed_at,
    )
    session.add(row)
    await session.flush()
    return row


# ---- pure: as-of / no forward leak -----------------------------------------


def test_extract_prediction_reads_ai_trend() -> None:
    direction, conf, eid = extract_prediction(
        {"aiTrend": {"direction": "down", "strength": 0.42}, "decisionEventId": "e1"}
    )
    assert direction == "down"
    assert conf == pytest.approx(0.42)
    assert eid == "e1"


def test_extract_prediction_clamps_strength_to_unit_interval() -> None:
    _, conf_hi, _ = extract_prediction({"aiTrend": {"direction": "up", "strength": 1.5}})
    _, conf_lo, _ = extract_prediction({"aiTrend": {"direction": "up", "strength": -0.2}})
    assert conf_hi == 1.0
    assert conf_lo == 0.0


def test_extract_prediction_defaults_when_missing() -> None:
    direction, conf, eid = extract_prediction({})
    assert direction == "flat"
    assert conf is None
    assert eid is None


def test_compute_realized_move_backward_asof_ignores_post_horizon_bars() -> None:
    # close 100 at signal, 110 at horizon, then a wild 999 bar AFTER the horizon.
    df = _frame(
        [
            {
                "time": "2026-06-06T00:00:00Z",
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 1,
            },
            {
                "time": "2026-06-09T00:00:00Z",
                "open": 110,
                "high": 110,
                "low": 110,
                "close": 110,
                "volume": 1,
            },
            {
                "time": "2026-06-10T00:00:00Z",
                "open": 999,
                "high": 999,
                "low": 999,
                "close": 999,
                "volume": 1,
            },
        ]
    )
    move = compute_realized_move_pct(
        df,
        datetime(2026, 6, 6, tzinfo=UTC),
        datetime(2026, 6, 9, tzinfo=UTC),
    )
    # 10%, NOT influenced by the post-horizon 999 bar → no look-ahead.
    assert move == pytest.approx(10.0)


def test_compute_realized_move_none_when_signal_before_history() -> None:
    df = _frame(
        [
            {
                "time": "2026-06-09T00:00:00Z",
                "open": 110,
                "high": 110,
                "low": 110,
                "close": 110,
                "volume": 1,
            },
        ]
    )
    move = compute_realized_move_pct(
        df,
        datetime(2026, 6, 1, tzinfo=UTC),  # before first bar → entry as-of is NaN
        datetime(2026, 6, 9, tzinfo=UTC),
    )
    assert move is None


# ---- record_candidate: idempotent ------------------------------------------


async def test_record_candidate_is_idempotent(
    db: async_sessionmaker[AsyncSession],
) -> None:
    svc = OaShadowService(market_data=_FakeMarketData(None))
    async with db() as session:
        history = await _history(session, completed_at=_NOW - timedelta(hours=96), history_id=1)
        first = await svc.record_candidate(session=session, history=history)
        second = await svc.record_candidate(session=session, history=history)
        await session.commit()

        assert first is not None and second is not None
        assert first.id == second.id
        rows = (await session.execute(select(OaShadowOutcome))).scalars().all()
        assert len(rows) == 1
        assert first.predicted_direction == "up"
        assert first.predicted_conf == pytest.approx(0.7)
        assert first.decision_event_id == "evt-x"
        assert first.realized_move_pct is None
        # horizon = signal + 72h
        assert first.horizon_end_utc == history.core_completed_at + timedelta(hours=72)


# ---- backfill: fills due, leaves future untouched --------------------------


class _StartAwareMarketData:
    """Fake that only returns bars at/after start_time (like a real exchange fetch)."""

    def __init__(self, candles: list[dict[str, Any]]) -> None:
        self._candles = candles
        self.calls: list[str] = []

    async def fetch_ohlcv(self, **kwargs: Any) -> Any:
        start_time = kwargs["start_time"]
        self.calls.append(start_time)
        cutoff = datetime.fromisoformat(start_time)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        kept = [
            c
            for c in self._candles
            if datetime.fromisoformat(c["time"].replace("Z", "+00:00")) >= cutoff
        ]
        return MarketDataService.frame_from_candles(kept)


async def test_backfill_resolves_entry_for_non_boundary_signal_via_lookback_pad(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Entry bar at 06-06 00:00, exit bar at 06-09 00:00. The signal is at 00:30 —
    # NOT on a 1h bar boundary. Without the signal_time-2h lookback pad the fetch
    # would start at 00:30 and drop the 00:00 entry bar → asof NaN → row skipped.
    candles = [
        {
            "time": "2026-06-06T00:00:00Z",
            "open": 100,
            "high": 100,
            "low": 100,
            "close": 100,
            "volume": 1,
        },
        {
            "time": "2026-06-09T00:00:00Z",
            "open": 110,
            "high": 110,
            "low": 110,
            "close": 110,
            "volume": 1,
        },
    ]
    market = _StartAwareMarketData(candles)
    svc = OaShadowService(market_data=market)
    async with db() as session:
        history = await _history(
            session,
            completed_at=datetime(2026, 6, 6, 0, 30, tzinfo=UTC),
            history_id=1,
        )
        await svc.record_candidate(session=session, history=history)
        await session.commit()

        stats = await svc.backfill_due(session=session, now=_NOW)
        await session.commit()

        assert stats["filled"] == 1  # pad made the entry bar available → not skipped
        row = await session.scalar(select(OaShadowOutcome).where(OaShadowOutcome.history_id == 1))
        assert row is not None
        assert row.realized_move_pct == pytest.approx(10.0)


async def test_backfill_fills_due_and_leaves_future_null(
    db: async_sessionmaker[AsyncSession],
) -> None:
    frame = _frame(
        [
            {
                "time": "2026-06-06T00:00:00Z",
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 1,
            },
            {
                "time": "2026-06-09T00:00:00Z",
                "open": 110,
                "high": 110,
                "low": 110,
                "close": 110,
                "volume": 1,
            },
            {
                "time": "2026-06-10T00:00:00Z",
                "open": 999,
                "high": 999,
                "low": 999,
                "close": 999,
                "volume": 1,
            },
        ]
    )
    market = _FakeMarketData(frame)
    svc = OaShadowService(market_data=market)
    async with db() as session:
        # due: signal -96h → horizon -24h ≤ NOW
        due = await _history(session, completed_at=_NOW - timedelta(hours=96), history_id=1)
        # future: signal -1h → horizon +71h > NOW
        future = await _history(session, completed_at=_NOW - timedelta(hours=1), history_id=2)
        await svc.record_candidate(session=session, history=due)
        await svc.record_candidate(session=session, history=future)
        await session.commit()

        stats = await svc.backfill_due(session=session, now=_NOW)
        await session.commit()

        assert stats["due"] == 1  # only the past-horizon row is due
        assert stats["filled"] == 1

        due_row = await session.scalar(
            select(OaShadowOutcome).where(OaShadowOutcome.history_id == 1)
        )
        future_row = await session.scalar(
            select(OaShadowOutcome).where(OaShadowOutcome.history_id == 2)
        )
        assert due_row is not None and future_row is not None
        assert due_row.realized_move_pct == pytest.approx(10.0)
        assert future_row.realized_move_pct is None
