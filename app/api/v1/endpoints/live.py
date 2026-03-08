from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, DbSession
from app.schemas.exchange_trading import SpotOrderCreate
from app.schemas.live import (
    AtrObSignalRunRequest,
    BuilderSignalRunRequest,
    LivePaperPlayStopResponse,
    LivePaperPollResponse,
    LivePaperProfileRead,
    LivePaperProfileUpsertRequest,
    LiveSignalResult,
    SignalExecuteRequest,
)
from app.services.execution.errors import ExchangeServiceError, error_http_status
from app.services.live_paper import LivePaperService
from app.services.execution.trading_service import TradingService
from app.services.live_signals import LiveSignalService

router = APIRouter()
signal_service = LiveSignalService()
trading_service = TradingService()
live_paper_service = LivePaperService()


async def _maybe_execute_signal(
    *,
    session: DbSession,
    current_user: CurrentUser,
    signal_result: dict[str, object],
    signal_symbol: str,
    execution: SignalExecuteRequest,
) -> dict[str, object]:
    if not bool(signal_result.get("has_signal")):
        return {}
    if not execution.execute:
        return {"mode": execution.mode, "status": "skipped"}
    entry = float(signal_result.get("entry") or 0.0)
    if entry <= 0:
        return {"mode": execution.mode, "status": "skipped", "reason": "invalid_entry"}
    side = str(signal_result.get("side") or "LONG")
    sizing = signal_result.get("sizing", {})
    if isinstance(sizing, dict):
        position_value = float(sizing.get("position_value", 0.0) or 0.0)
    else:
        position_value = 0.0
    if position_value <= 0:
        return {"mode": execution.mode, "status": "skipped", "reason": "invalid_position_value"}
    if execution.mode == "live":
        if execution.account_id is None:
            raise HTTPException(status_code=422, detail="account_id is required for live execution")
        payload = SpotOrderCreate(
            account_id=execution.account_id,
            symbol=signal_symbol,
            side="buy" if side == "LONG" else "sell",
            order_type="market",
            amount=position_value / entry,
        )
        order = await trading_service.place_spot_order(
            session=session,
            user_id=current_user.id,
            payload=payload,
        )
        return {"mode": "live", "status": "submitted", "order_id": order.order.id}
    return {"mode": "dry_run", "status": "simulated"}


@router.post("/signals/builder", response_model=LiveSignalResult, summary="Compute builder live signal")
async def run_builder_signal(
    payload: BuilderSignalRunRequest,
    session: DbSession,
    current_user: CurrentUser,
) -> LiveSignalResult:
    signal = await signal_service.compute_builder_signal(payload.signal.model_dump())
    try:
        execution = await _maybe_execute_signal(
            session=session,
            current_user=current_user,
            signal_result=signal,
            signal_symbol=payload.signal.symbol,
            execution=payload.execution,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc
    signal["execution"] = execution
    return LiveSignalResult(**signal)


@router.post(
    "/signals/atr-order-block",
    response_model=LiveSignalResult,
    summary="Compute ATR order-block live signal",
)
async def run_atr_ob_signal(
    payload: AtrObSignalRunRequest,
    session: DbSession,
    current_user: CurrentUser,
) -> LiveSignalResult:
    signal = await signal_service.compute_atr_ob_signal(payload.signal.model_dump())
    try:
        execution = await _maybe_execute_signal(
            session=session,
            current_user=current_user,
            signal_result=signal,
            signal_symbol=payload.signal.symbol,
            execution=payload.execution,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc
    signal["execution"] = execution
    return LiveSignalResult(**signal)


@router.put(
    "/paper/profile",
    response_model=LivePaperProfileRead,
    summary="Create or update live paper profile",
)
async def upsert_live_paper_profile(
    payload: LivePaperProfileUpsertRequest,
    session: DbSession,
    current_user: CurrentUser,
) -> LivePaperProfileRead:
    try:
        profile = await live_paper_service.upsert_profile(
            session=session,
            user_id=current_user.id,
            payload=payload,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LivePaperProfileRead.model_validate(profile)


@router.post(
    "/paper/play",
    response_model=LivePaperPlayStopResponse,
    summary="Enable live paper mode",
)
async def play_live_paper(session: DbSession, current_user: CurrentUser) -> LivePaperPlayStopResponse:
    try:
        profile = await live_paper_service.set_running(
            session=session,
            user_id=current_user.id,
            is_running=True,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return LivePaperPlayStopResponse(profile=LivePaperProfileRead.model_validate(profile))


@router.post(
    "/paper/stop",
    response_model=LivePaperPlayStopResponse,
    summary="Disable live paper mode",
)
async def stop_live_paper(session: DbSession, current_user: CurrentUser) -> LivePaperPlayStopResponse:
    try:
        profile = await live_paper_service.set_running(
            session=session,
            user_id=current_user.id,
            is_running=False,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return LivePaperPlayStopResponse(profile=LivePaperProfileRead.model_validate(profile))


@router.get(
    "/paper/poll",
    response_model=LivePaperPollResponse,
    summary="Poll latest live paper trades and events",
)
async def poll_live_paper(
    session: DbSession,
    current_user: CurrentUser,
    last_trade_id: int | None = None,
    last_event_id: int | None = None,
    limit: int = 500,
) -> LivePaperPollResponse:
    try:
        (
            profile,
            live_trades_since_start,
            events,
            metrics,
        ) = await live_paper_service.poll_profile(
            session=session,
            user_id=current_user.id,
            last_trade_id=last_trade_id,
            last_event_id=last_event_id,
            limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LivePaperPollResponse(
        profile=LivePaperProfileRead.model_validate(profile),
        live_trades_since_start=[trade for trade in live_trades_since_start],
        events=[event for event in events],
        metrics=metrics,
    )
