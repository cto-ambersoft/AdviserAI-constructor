"""Outcome-Aware shadow outcomes (S2).

Records every personal-analysis forecast as a counterfactual ("shadow") outcome
and, once the horizon closes, backfills the realized market move from OHLCV using
point-in-time (backward as-of) lookups. This is what lets OA later learn from the
trades it would have skipped — defeating selection/censoring bias (spec §5.3, D4).

The realized-move computation is deliberately a pure function so the as-of /
no-forward-leak guarantee is unit-testable without any network or DB.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auto_trade_position import AutoTradePosition
from app.models.oa_shadow_outcome import OaShadowOutcome
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.services.market_data.service import MarketDataService

logger = logging.getLogger(__name__)

# Evaluation horizon for a forecast, in hours. Single value in v1 (spec OQ4).
OA_SHADOW_HORIZON_HOURS = 72
# OHLCV granularity / depth used to resolve the realized move over the horizon.
_BACKFILL_TIMEFRAME = "1h"
_BACKFILL_EXCHANGE = "binance"
# 72h horizon at 1h needs ~72 bars; pad generously for as-of alignment slack.
_BACKFILL_BARS = 200
# Fetch a couple of bars before the signal so a backward as-of entry bar always
# exists even when the signal doesn't land exactly on a bar boundary.
_BACKFILL_ENTRY_PAD = timedelta(hours=2)


def _to_utc_ts(value: datetime) -> pd.Timestamp:
    """Coerce a datetime to a UTC-aware pandas Timestamp.

    OHLCV frame indexes are tz-aware UTC; horizon/signal timestamps read back from
    SQLite can be tz-naive, so normalise both sides before any comparison.
    """
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def extract_prediction(analysis_data: dict[str, object]) -> tuple[str, float | None, str | None]:
    """Pull (direction, confidence, decision_event_id) out of a core result JSON."""
    ai_trend = analysis_data.get("aiTrend")
    direction = "flat"
    confidence: float | None = None
    if isinstance(ai_trend, dict):
        raw_direction = ai_trend.get("direction")
        if isinstance(raw_direction, str) and raw_direction:
            direction = raw_direction
        raw_strength = ai_trend.get("strength")
        if isinstance(raw_strength, (int, float)):
            # Calibration consumes confidence as a probability — clamp to [0, 1] so a
            # stray out-of-range strength can't desync from the executed-side scale.
            confidence = max(0.0, min(1.0, float(raw_strength)))
    decision_event_id = analysis_data.get("decisionEventId")
    if not isinstance(decision_event_id, str):
        decision_event_id = None
    return direction, confidence, decision_event_id


def compute_realized_move_pct(
    df: pd.DataFrame,
    signal_time: datetime,
    horizon_end: datetime,
) -> float | None:
    """Realized % move from ``signal_time`` to ``horizon_end`` (backward as-of).

    Only bars at or before ``horizon_end`` are ever consulted, so a forecast's
    realized move can never be contaminated by data published after its horizon
    (no look-ahead). Returns ``None`` when there isn't enough history to resolve
    both endpoints or the entry price is non-positive.
    """
    if df is None or df.empty or "close" not in df.columns:
        return None

    signal_ts = _to_utc_ts(signal_time)
    horizon_ts = _to_utc_ts(horizon_end)

    # Hard cut everything after the horizon: the realized outcome is, by
    # construction, only what was knowable by the end of the evaluation window.
    bounded = df[df.index <= horizon_ts]
    if bounded.empty:
        return None

    close = bounded["close"]
    entry: Any = close.asof(signal_ts)
    exit_price: Any = close.asof(horizon_ts)
    if pd.isna(entry) or pd.isna(exit_price) or float(entry) <= 0:
        return None

    return (float(exit_price) - float(entry)) / float(entry) * 100.0


class OaShadowService:
    """Writes shadow candidates and backfills their realized move."""

    def __init__(self, market_data: MarketDataService | None = None) -> None:
        self._market_data = market_data or MarketDataService()

    async def record_candidate(
        self,
        *,
        session: AsyncSession,
        history: PersonalAnalysisHistory,
    ) -> OaShadowOutcome | None:
        """Idempotently record one forecast as a shadow candidate (uq history_id)."""
        existing = await session.scalar(
            select(OaShadowOutcome).where(OaShadowOutcome.history_id == history.id)
        )
        if existing is not None:
            return existing

        # Prefer the upstream compute time; fall back to the persisted forecast time
        # (created_at), never wall-clock now() — the horizon must start at forecast
        # time, not at record time.
        signal_time = history.core_completed_at or history.created_at or datetime.now(UTC)
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=UTC)
        direction, confidence, decision_event_id = extract_prediction(history.analysis_data)

        row = OaShadowOutcome(
            user_id=history.user_id,
            profile_id=history.profile_id,
            history_id=history.id,
            symbol=history.symbol,
            decision_event_id=decision_event_id,
            signal_time_utc=signal_time,
            horizon_end_utc=signal_time + timedelta(hours=OA_SHADOW_HORIZON_HOURS),
            predicted_direction=direction,
            predicted_conf=confidence,
            realized_move_pct=None,
            entered=False,
        )
        session.add(row)
        await session.flush()
        return row

    async def backfill_due(
        self,
        *,
        session: AsyncSession,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Fill realized_move_pct for shadow rows whose horizon has closed.

        Future-horizon rows are left untouched (their outcome isn't knowable yet).
        Per-row failures are isolated so one bad symbol can't stall the batch.
        """
        now = now or datetime.now(UTC)
        stats = {"due": 0, "filled": 0, "skipped": 0, "errors": 0}

        # Lock the due rows (skip already-locked) so overlapping backfill workers
        # partition the batch instead of double-fetching OHLCV for the same rows.
        # No-op on SQLite (tests), native row-lock on Postgres.
        due_stmt = select(OaShadowOutcome).where(
            OaShadowOutcome.realized_move_pct.is_(None),
            OaShadowOutcome.horizon_end_utc <= now,
        )
        bind = session.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", "") != "sqlite":
            due_stmt = due_stmt.with_for_update(skip_locked=True)
        due_rows = (await session.execute(due_stmt)).scalars().all()
        stats["due"] = len(due_rows)

        for row in due_rows:
            try:
                # Start the fetch one bar BEFORE the signal so an at-or-before
                # entry bar exists for the backward as-of lookup (a signal at 00:30
                # with 1h bars needs the 00:00 bar). Without the pad the entry asof
                # could be NaN and the row would be silently skipped.
                df = await self._market_data.fetch_ohlcv(
                    exchange_name=_BACKFILL_EXCHANGE,
                    symbol=row.symbol,
                    timeframe=_BACKFILL_TIMEFRAME,
                    bars=_BACKFILL_BARS,
                    market_type="futures",
                    start_time=(row.signal_time_utc - _BACKFILL_ENTRY_PAD).isoformat(),
                    end_time=row.horizon_end_utc.isoformat(),
                )
                move = compute_realized_move_pct(df, row.signal_time_utc, row.horizon_end_utc)
            except Exception as exc:  # noqa: BLE001 - isolate per-row failures
                logger.warning(
                    "oa_shadow backfill failed for history_id=%s: %s",
                    row.history_id,
                    exc,
                )
                stats["errors"] += 1
                continue

            if move is None:
                stats["skipped"] += 1
                continue

            row.realized_move_pct = move
            row.entered = await self._history_was_entered(session, row.history_id)
            stats["filled"] += 1

        return stats

    @staticmethod
    async def _history_was_entered(session: AsyncSession, history_id: int) -> bool:
        """True if any auto-trade position was opened from this forecast."""
        position_id = await session.scalar(
            select(AutoTradePosition.id).where(AutoTradePosition.open_history_id == history_id)
        )
        return position_id is not None
