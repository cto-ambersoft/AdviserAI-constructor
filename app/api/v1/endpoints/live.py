from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, DbSession
from app.schemas.auto_trade import (
    AutoTradeConfigRead,
    AutoTradeConfigsResponse,
    AutoTradeConfigUpsertRequest,
    AutoTradeEventRead,
    AutoTradeEventsResponse,
    AutoTradeLedgerTradeRead,
    AutoTradeLedgerTradesResponse,
    AutoTradeLedgerTradesSummaryRead,
    AutoTradePlayStopResponse,
    AutoTradePositionPnlRead,
    AutoTradePositionRead,
    AutoTradePositionsResponse,
    AutoTradePositionsSummaryRead,
    AutoTradePositionWithPnlRead,
    AutoTradeStateResponse,
)
from app.schemas.exchange_trading import SpotOrderCreate
from app.schemas.live import (
    AtrObSignalRunRequest,
    BuilderSignalRunRequest,
    LivePaperEventRead,
    LivePaperPlayStopResponse,
    LivePaperPollResponse,
    LivePaperProfileRead,
    LivePaperProfileUpsertRequest,
    LivePaperTradeRead,
    LiveSignalResult,
    SignalExecuteRequest,
)
from app.services.auto_trade.service import AutoTradeService
from app.services.auto_trade.trade_sync import ExchangeTradeSyncService
from app.services.execution.errors import ExchangeServiceError, error_http_status
from app.services.execution.trading_service import TradingService
from app.services.live_paper import LivePaperService
from app.services.live_signals import LiveSignalService

router = APIRouter()
signal_service = LiveSignalService()
trading_service = TradingService()
live_paper_service = LivePaperService()
auto_trade_service = AutoTradeService()
trade_sync_service = ExchangeTradeSyncService()


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
    entry_value = signal_result.get("entry")
    entry = float(entry_value) if isinstance(entry_value, (int, float, str)) else 0.0
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


@router.post(
    "/signals/builder",
    response_model=LiveSignalResult,
    summary="Compute builder live signal",
)
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
async def play_live_paper(
    session: DbSession,
    current_user: CurrentUser,
) -> LivePaperPlayStopResponse:
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
async def stop_live_paper(
    session: DbSession,
    current_user: CurrentUser,
) -> LivePaperPlayStopResponse:
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
        live_trades_since_start=[
            LivePaperTradeRead.model_validate(trade) for trade in live_trades_since_start
        ],
        events=[LivePaperEventRead.model_validate(event) for event in events],
        metrics=metrics,
    )


@router.get(
    "/auto-trade/configs",
    response_model=AutoTradeConfigsResponse,
    summary="List auto-trade configs",
)
async def list_auto_trade_configs(
    session: DbSession,
    current_user: CurrentUser,
) -> AutoTradeConfigsResponse:
    rows = await auto_trade_service.list_configs(session=session, user_id=current_user.id)
    active_row = next((row for row in rows if row.is_running), None)
    if active_row is None and rows:
        active_row = rows[0]
    return AutoTradeConfigsResponse(
        configs=[AutoTradeConfigRead.model_validate(row) for row in rows],
        active_account_id=active_row.account_id if active_row is not None else None,
        active_config=(
            AutoTradeConfigRead.model_validate(active_row) if active_row is not None else None
        ),
    )


@router.get(
    "/auto-trade/config",
    response_model=AutoTradeConfigRead,
    summary="Get auto-trade config",
)
async def get_auto_trade_config(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int | None = Query(default=None, ge=1),
) -> AutoTradeConfigRead:
    try:
        row = await auto_trade_service.get_config(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            fail_on_ambiguous=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Auto-trade config not found.")
    return AutoTradeConfigRead.model_validate(row)


@router.put(
    "/auto-trade/config",
    response_model=AutoTradeConfigRead,
    summary="Create or update auto-trade config",
)
async def upsert_auto_trade_config(
    payload: AutoTradeConfigUpsertRequest,
    session: DbSession,
    current_user: CurrentUser,
) -> AutoTradeConfigRead:
    try:
        row = await auto_trade_service.upsert_config(
            session=session,
            user_id=current_user.id,
            payload=payload,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AutoTradeConfigRead.model_validate(row)


@router.post(
    "/auto-trade/play",
    response_model=AutoTradePlayStopResponse,
    summary="Enable auto-trade execution",
)
async def play_auto_trade(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int | None = Query(default=None, ge=1),
) -> AutoTradePlayStopResponse:
    try:
        row = await auto_trade_service.set_running(
            session=session,
            user_id=current_user.id,
            is_running=True,
            account_id=account_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AutoTradePlayStopResponse(config=AutoTradeConfigRead.model_validate(row))


@router.post(
    "/auto-trade/stop",
    response_model=AutoTradePlayStopResponse,
    summary="Disable auto-trade execution",
)
async def stop_auto_trade(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int | None = Query(default=None, ge=1),
) -> AutoTradePlayStopResponse:
    try:
        row = await auto_trade_service.set_running(
            session=session,
            user_id=current_user.id,
            is_running=False,
            account_id=account_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AutoTradePlayStopResponse(config=AutoTradeConfigRead.model_validate(row))


@router.get(
    "/auto-trade/state",
    response_model=AutoTradeStateResponse,
    summary="Get auto-trade runtime state",
)
async def get_auto_trade_state(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int | None = Query(default=None, ge=1),
) -> AutoTradeStateResponse:
    try:
        config = await auto_trade_service.get_config(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            fail_on_ambiguous=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if config is None:
        return AutoTradeStateResponse(config=None)
    return AutoTradeStateResponse(
        config=AutoTradeConfigRead.model_validate(config),
    )


@router.get(
    "/auto-trade/positions",
    response_model=AutoTradePositionsResponse,
    summary="Get auto-trade positions with PnL summary",
)
async def get_auto_trade_positions(
    session: DbSession,
    current_user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
    status: Literal["open", "closed", "error"] | None = Query(default=None),
    account_id: int | None = Query(default=None, ge=1),
) -> AutoTradePositionsResponse:
    try:
        config = await auto_trade_service.get_config(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            fail_on_ambiguous=True,
        )
        if config is not None:
            try:
                await trade_sync_service.sync_config_trades(session=session, config=config)
            except Exception:
                await session.rollback()
                pass
        payload = await auto_trade_service.summarize_positions_pnl(
            session=session,
            user_id=current_user.id,
            limit=limit,
            status=status,
            account_id=account_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AutoTradePositionsResponse(
        positions=[
            AutoTradePositionWithPnlRead(
                position=AutoTradePositionRead.model_validate(item["position"]),
                pnl=AutoTradePositionPnlRead(**item["pnl"]),
                lifecycle=item.get("lifecycle", {}),
                trade_pnl_usdt=item.get("trade_pnl_usdt"),
            )
            for item in payload["positions"]
        ],
        summary=AutoTradePositionsSummaryRead(**payload["summary"]),
    )


@router.get(
    "/auto-trade/trades",
    response_model=AutoTradeLedgerTradesResponse,
    summary="Get synchronized auto-trade futures fills",
)
async def get_auto_trade_trades(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
    symbol: str | None = Query(default=None),
    origin: Literal["platform", "external", "unknown"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> AutoTradeLedgerTradesResponse:
    config = await auto_trade_service.get_config(
        session=session,
        user_id=current_user.id,
        account_id=account_id,
        fail_on_ambiguous=False,
    )
    if config is not None:
        try:
            await trade_sync_service.sync_config_trades(session=session, config=config)
        except Exception:
            await session.rollback()
            pass
    rows = await trade_sync_service.list_trades(
        session=session,
        user_id=current_user.id,
        account_id=account_id,
        symbol=symbol,
        origin=origin,
        limit=limit,
    )
    platform = sum(1 for row in rows if row.origin == "platform")
    external = sum(1 for row in rows if row.origin == "external")
    total_fee = 0.0
    for row in rows:
        if (row.fee_currency or "").upper() == "USDT":
            total_fee += float(row.fee_cost)
    return AutoTradeLedgerTradesResponse(
        trades=[AutoTradeLedgerTradeRead.model_validate(item) for item in rows],
        summary=AutoTradeLedgerTradesSummaryRead(
            total=len(rows),
            platform=platform,
            external=external,
            total_fee_usdt=total_fee,
        ),
    )


@router.get(
    "/auto-trade/events",
    response_model=AutoTradeEventsResponse,
    summary="Get auto-trade events",
)
async def get_auto_trade_events(
    session: DbSession,
    current_user: CurrentUser,
    limit: int = 50,
    account_id: int | None = Query(default=None, ge=1),
) -> AutoTradeEventsResponse:
    bounded_limit = max(1, min(limit, 200))
    try:
        rows = await auto_trade_service.list_events(
            session=session,
            user_id=current_user.id,
            limit=bounded_limit,
            account_id=account_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AutoTradeEventsResponse(
        events=[AutoTradeEventRead.model_validate(item) for item in rows]
    )
