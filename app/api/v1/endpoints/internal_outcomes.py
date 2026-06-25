"""Internal: real trade outcomes keyed by core ai_decision_events id (T10a / W3a).

The core service computes agent accuracy by joining its ``ai_decision_events`` to the
actual trade each decision drove, via ``auto_trade_position.decision_event_id``. This
endpoint returns the realized entry->close market move for closed positions in a
window — the real analog of the synthetic daily-price move the core used before.
Internal-API-key authed (same scheme as the backtest internal endpoints).
"""

import hmac
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import DbSession
from app.core.config import get_settings
from app.models.auto_trade_position import AutoTradePosition

router = APIRouter()


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
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
) -> dict[str, list[dict[str, object]]]:
    _assert_internal_key(x_internal_api_key)
    since = datetime.now(UTC) - timedelta(days=since_days)
    rows = (
        await session.execute(
            select(AutoTradePosition).where(
                AutoTradePosition.status == "closed",
                AutoTradePosition.decision_event_id.is_not(None),
                AutoTradePosition.close_price.is_not(None),
                AutoTradePosition.closed_at >= since,
            )
        )
    ).scalars().all()

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
                "symbol": position.symbol,
                "side": position.side,
                "realized_move_pct": realized_move_pct,
                "closed_at": position.closed_at.isoformat() if position.closed_at else None,
            }
        )
    return {"outcomes": outcomes}
