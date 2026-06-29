"""Internal: real (and counterfactual) trade outcomes keyed by core decision id.

The core service computes agent accuracy by joining its ``ai_decision_events`` to the
actual trade each decision drove, via ``auto_trade_position.decision_event_id``. This
endpoint returns the realized entry->close market move for closed positions in a
window — the real analog of the synthetic daily-price move the core used before.

With ``include_shadow=true`` (used by the Outcome-Aware loop) it ALSO returns
counterfactual "shadow" outcomes for forecasts that were never entered (from
``oa_shadow_outcomes``), tagged with ``user_id``/``profile_id``/``predicted_direction``/
``predicted_conf`` and ``entered``, so OA accuracy/calibration aren't censored to the
trades that happened to be taken. Internal-API-key authed (constant-time check).
"""

import hmac
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import DbSession
from app.core.config import get_settings
from app.models.auto_trade_position import AutoTradePosition
from app.models.oa_shadow_outcome import OaShadowOutcome

router = APIRouter()


def _normalize_conf(value: float | None) -> float | None:
    """Clamp a confidence to [0, 1] so executed and shadow rows share one scale."""
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _side_to_direction(side: str | None) -> str | None:
    """Map an executed position side to the forecast direction it represents."""
    if side is None:
        return None
    upper = side.upper()
    if upper in {"LONG", "BUY"}:
        return "up"
    if upper in {"SHORT", "SELL"}:
        return "down"
    return None


def _assert_internal_key(raw_key: str | None) -> None:
    expected = get_settings().internal_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API key is not configured.",
        )
    if not hmac.compare_digest(raw_key or "", expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key.",
        )


@router.get("/agent-outcomes", summary="Realized trade outcomes by decision event id")
async def agent_outcomes(
    session: DbSession,
    since_days: int = Query(default=30, ge=1, le=365),
    include_shadow: bool = Query(
        default=False,
        description="Also return counterfactual outcomes for forecasts never entered (OA).",
    ),
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
) -> dict[str, list[dict[str, object]]]:
    _assert_internal_key(x_internal_api_key)
    since = datetime.now(UTC) - timedelta(days=since_days)
    rows = (
        (
            await session.execute(
                select(AutoTradePosition).where(
                    AutoTradePosition.status == "closed",
                    AutoTradePosition.decision_event_id.is_not(None),
                    AutoTradePosition.close_price.is_not(None),
                    AutoTradePosition.closed_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )

    outcomes: list[dict[str, object]] = []
    for position in rows:
        entry = position.entry_price
        close = position.close_price
        if not entry or entry <= 0 or close is None:
            continue
        realized_move_pct = (close - entry) / entry * 100.0
        outcomes.append(
            {
                "decision_event_id": position.decision_event_id,
                "user_id": position.user_id,
                "profile_id": position.profile_id,
                "symbol": position.symbol,
                "side": position.side,
                "predicted_direction": _side_to_direction(position.side),
                # Forecast confidence the calibration layer consumes, normalized to
                # 0-1 (position stores it as a percent).
                "predicted_conf": (
                    _normalize_conf(position.entry_confidence_pct / 100.0)
                    if position.entry_confidence_pct is not None
                    else None
                ),
                "realized_move_pct": realized_move_pct,
                "closed_at": position.closed_at.isoformat() if position.closed_at else None,
                # Executed: a real position was opened from this decision.
                "entered": True,
            }
        )

    if include_shadow:
        # Window anchor: executed rows are windowed by `closed_at`, shadow rows by
        # `horizon_end_utc` — both are the outcome-RESOLUTION time ("when the result
        # became known"), so the two sets cover the same period in resolution terms.
        shadow_rows = (
            (
                await session.execute(
                    select(OaShadowOutcome).where(
                        OaShadowOutcome.realized_move_pct.is_not(None),
                        OaShadowOutcome.horizon_end_utc >= since,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Exclude forecasts that actually became positions (represented by the
        # executed rows above) — bounded to just the candidate shadow history_ids,
        # so this never full-scans the whole positions table.
        candidate_ids = {row.history_id for row in shadow_rows}
        entered_history_ids: set[int | None] = set()
        if candidate_ids:
            entered_history_ids = set(
                (
                    await session.execute(
                        select(AutoTradePosition.open_history_id).where(
                            AutoTradePosition.open_history_id.in_(candidate_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
        for shadow in shadow_rows:
            if shadow.history_id in entered_history_ids:
                continue
            outcomes.append(
                {
                    "decision_event_id": shadow.decision_event_id,
                    "user_id": shadow.user_id,
                    "profile_id": shadow.profile_id,
                    "symbol": shadow.symbol,
                    "side": None,
                    "predicted_direction": shadow.predicted_direction,
                    "predicted_conf": _normalize_conf(shadow.predicted_conf),
                    "realized_move_pct": shadow.realized_move_pct,
                    "closed_at": shadow.horizon_end_utc.isoformat(),
                    # Counterfactual: this forecast was never entered.
                    "entered": False,
                }
            )

    return {"outcomes": outcomes}
