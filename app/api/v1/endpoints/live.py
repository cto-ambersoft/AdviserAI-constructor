from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, DbSession, RequireStepUp
from app.schemas.ai_overlay import (
    AiOverlayConfig,
    AiOverlayConfigResponse,
    AiOverlayConfigUpdateRequest,
)
from app.schemas.auto_trade import (
    AccountBalanceResponse,
    AutoTradeCloseOpenPositionsRequest,
    AutoTradeCloseOpenPositionsResponse,
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
    AutoTradeRiskConfig,
    AutoTradeStateResponse,
    BulkLifecycleResponse,
    BulkLifecycleResultItem,
    PortfolioSummaryResponse,
    PositionTraceRead,
    PromotionStatusRead,
    RiskConfigBulkApplyResponse,
    StrategyHealthRead,
    StrategyPortfolioEntryRead,
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
from app.schemas.notifications import (
    TelegramLinkOut,
    TelegramSettingsOut,
    TelegramSettingsUpdate,
    TelegramTestResult,
)
from app.services.auto_trade.health import compute_strategy_health
from app.services.auto_trade.portfolio import compute_portfolio
from app.services.auto_trade.promotion import InvalidPromotionError
from app.services.auto_trade.service import (
    AutoTradeService,
    ConfirmationRequiredError,
    PromotionGateError,
)
from app.services.auto_trade.trade_sync import ExchangeTradeSyncService
from app.services.execution.errors import ExchangeServiceError, error_http_status
from app.services.execution.futures_pnl import sum_fee_cost_quote
from app.services.execution.trading_service import TradingService
from app.services.live_paper import LivePaperService
from app.services.live_signals import LiveSignalService
from app.services.notifications.service import TelegramNotificationService

router = APIRouter()
signal_service = LiveSignalService()
trading_service = TradingService()
live_paper_service = LivePaperService()
auto_trade_service = AutoTradeService()
trade_sync_service = ExchangeTradeSyncService()
telegram_notify_service = TelegramNotificationService()


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
        # §3: route the manual order through the account's pre-trade risk envelope
        # (the same supervisor that gates automated entries). Fail-safe: an account
        # with no auto-trade config / no risk row, or a reducing order, is allowed
        # unchanged — this never tightens behaviour for un-configured manual trading.
        risk_decision = await auto_trade_service.precheck_manual_order(
            session=session,
            user_id=current_user.id,
            account_id=execution.account_id,
            symbol=signal_symbol,
            side=side,
            price=entry,
        )
        if not risk_decision.allowed:
            return {
                "mode": "live",
                "status": "blocked",
                "reason": risk_decision.reason,
                "rule": risk_decision.rule,
            }
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
        configs=[
            await auto_trade_service.serialize_config(session=session, config=row) for row in rows
        ],
        active_account_id=active_row.account_id if active_row is not None else None,
        active_config=(
            await auto_trade_service.serialize_config(session=session, config=active_row)
            if active_row is not None
            else None
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
    config_id: int | None = Query(default=None, ge=1),
) -> AutoTradeConfigRead:
    try:
        row = await auto_trade_service.get_config(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            config_id=config_id,
            fail_on_ambiguous=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Auto-trade config not found.")
    return await auto_trade_service.serialize_config(session=session, config=row)


@router.put(
    "/auto-trade/config",
    response_model=AutoTradeConfigRead,
    summary="Create or update auto-trade config",
)
async def upsert_auto_trade_config(
    payload: AutoTradeConfigUpsertRequest,
    session: DbSession,
    current_user: RequireStepUp,
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
    return await auto_trade_service.serialize_config(session=session, config=row)


@router.patch(
    "/auto-trade/risk-config/apply-all",
    response_model=RiskConfigBulkApplyResponse,
    summary="Apply one risk config to all of the user's strategies (step-up required)",
)
async def apply_risk_config_to_all_strategies(
    payload: AutoTradeRiskConfig,
    session: DbSession,
    current_user: RequireStepUp,
) -> RiskConfigBulkApplyResponse:
    try:
        updated = await auto_trade_service.apply_risk_config_to_all_strategies(
            session=session,
            user_id=current_user.id,
            risk=payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RiskConfigBulkApplyResponse(updated_count=updated)


@router.post(
    "/auto-trade/config/{config_id}/rollback/{revision_id}",
    response_model=AutoTradeConfigRead,
    summary="Roll a strategy config back to a prior revision (step-up required)",
)
async def rollback_auto_trade_config(
    session: DbSession,
    current_user: RequireStepUp,
    config_id: int = Path(ge=1),
    revision_id: int = Path(ge=1),
) -> AutoTradeConfigRead:
    try:
        row = await auto_trade_service.rollback_config(
            session=session,
            user_id=current_user.id,
            config_id=config_id,
            revision_id=revision_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await auto_trade_service.serialize_config(session=session, config=row)


@router.get(
    "/auto-trade/strategies/{config_id}/promotion-status",
    response_model=PromotionStatusRead,
    summary="Promotion KPI-Gate readiness for a strategy",
)
async def get_strategy_promotion_status(
    session: DbSession,
    current_user: CurrentUser,
    config_id: int = Path(ge=1),
) -> PromotionStatusRead:
    try:
        return await auto_trade_service.get_promotion_status(
            session=session, user_id=current_user.id, config_id=config_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/auto-trade/strategies/{config_id}/promote",
    response_model=AutoTradeConfigRead,
    summary="Promote a sandbox strategy to live (step-up required)",
)
async def promote_strategy(
    session: DbSession,
    current_user: RequireStepUp,
    config_id: int = Path(ge=1),
) -> AutoTradeConfigRead:
    try:
        row = await auto_trade_service.promote_strategy(
            session=session, user_id=current_user.id, config_id=config_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PromotionGateError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Promotion gate not satisfied.",
                "failed": [
                    {"name": c.name, "actual": c.actual, "threshold": c.threshold}
                    for c in exc.decision.failed
                ],
            },
        ) from exc
    except InvalidPromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await auto_trade_service.serialize_config(session=session, config=row)


@router.post(
    "/auto-trade/strategies/{config_id}/demote",
    response_model=AutoTradeConfigRead,
    summary="Demote a live strategy back to sandbox (step-up required)",
)
async def demote_strategy(
    session: DbSession,
    current_user: RequireStepUp,
    config_id: int = Path(ge=1),
) -> AutoTradeConfigRead:
    try:
        row = await auto_trade_service.demote_strategy(
            session=session, user_id=current_user.id, config_id=config_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidPromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await auto_trade_service.serialize_config(session=session, config=row)


@router.get(
    "/auto-trade/ai-overlay/config",
    response_model=AiOverlayConfigResponse,
    summary="Get AI trend overlay config",
)
async def get_ai_overlay_config(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int | None = Query(default=None, ge=1),
    config_id: int | None = Query(default=None, ge=1),
) -> AiOverlayConfigResponse:
    try:
        row = await auto_trade_service.get_config(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            config_id=config_id,
            fail_on_ambiguous=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Auto-trade config not found.")
    config = AiOverlayConfig.from_record(row.ai_overlay_config_json)
    return AiOverlayConfigResponse(config=config)


@router.put(
    "/auto-trade/ai-overlay/config",
    response_model=AiOverlayConfigResponse,
    summary="Update AI trend overlay config",
)
async def update_ai_overlay_config(
    payload: AiOverlayConfigUpdateRequest,
    session: DbSession,
    current_user: CurrentUser,
    account_id: int | None = Query(default=None, ge=1),
    config_id: int | None = Query(default=None, ge=1),
) -> AiOverlayConfigResponse:
    try:
        row = await auto_trade_service.get_config(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            config_id=config_id,
            fail_on_ambiguous=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Auto-trade config not found.")

    current = AiOverlayConfig.from_record(row.ai_overlay_config_json)
    try:
        merged = payload.merge_into(current)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row.ai_overlay_config_json = merged.to_record()
    await session.commit()
    return AiOverlayConfigResponse(config=merged)


@router.post(
    "/auto-trade/play",
    response_model=AutoTradePlayStopResponse,
    summary="Enable auto-trade execution",
)
async def play_auto_trade(
    session: DbSession,
    current_user: RequireStepUp,
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
    return AutoTradePlayStopResponse(
        config=await auto_trade_service.serialize_config(session=session, config=row)
    )


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
    return AutoTradePlayStopResponse(
        config=await auto_trade_service.serialize_config(session=session, config=row)
    )


@router.post(
    "/auto-trade/close-positions",
    response_model=AutoTradeCloseOpenPositionsResponse,
    summary="Manually flatten every open auto-trade position",
    description=(
        "Two-step destructive operation. First call with ``confirm: false`` "
        "returns HTTP 412 with a preview of every position that would be "
        "closed (symbol, side, quantity, conditional-order count). Re-send "
        "with ``confirm: true`` to actually cancel TP/SL and market-close. "
        "Independent from ``/auto-trade/stop``: this does not flip "
        "``is_running``."
    ),
    responses={
        412: {
            "description": (
                "Confirmation required. Body contains an "
                "``AutoTradeClosePreview`` payload."
            ),
        },
        404: {"description": "Auto-trade config not found for the given scope."},
    },
)
async def close_auto_trade_positions(
    payload: AutoTradeCloseOpenPositionsRequest,
    session: DbSession,
    current_user: CurrentUser,
) -> AutoTradeCloseOpenPositionsResponse | JSONResponse:
    try:
        return await auto_trade_service.close_open_positions(
            session=session,
            user_id=current_user.id,
            account_id=payload.account_id,
            confirm=payload.confirm,
            reason=payload.reason,
        )
    except ConfirmationRequiredError as exc:
        return JSONResponse(
            status_code=412,
            content=exc.preview.model_dump(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/auto-trade/state",
    response_model=AutoTradeStateResponse,
    summary="Get auto-trade runtime state",
)
async def get_auto_trade_state(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int | None = Query(default=None, ge=1),
    config_id: int | None = Query(default=None, ge=1),
) -> AutoTradeStateResponse:
    try:
        config = await auto_trade_service.get_config(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            config_id=config_id,
            fail_on_ambiguous=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if config is None:
        return AutoTradeStateResponse(config=None)
    return AutoTradeStateResponse(
        config=await auto_trade_service.serialize_config(session=session, config=config),
    )


@router.get(
    "/auto-trade/strategies/{config_id}/health",
    response_model=StrategyHealthRead,
    summary="Strategy health score (composite of win rate, drawdown, PnL, stability)",
)
async def get_strategy_health(
    session: DbSession,
    current_user: CurrentUser,
    config_id: int = Path(ge=1),
    window_days: int = Query(default=30, ge=1, le=365),
) -> StrategyHealthRead:
    config = await auto_trade_service.get_config(
        session=session,
        user_id=current_user.id,
        account_id=None,
        config_id=config_id,
        fail_on_ambiguous=True,
    )
    if config is None:
        raise HTTPException(status_code=404, detail="Auto-trade config not found.")
    health = await compute_strategy_health(
        session=session,
        config_id=config_id,
        window_days=window_days,
    )
    return StrategyHealthRead.model_validate(health)


@router.get(
    "/auto-trade/positions/{position_id}/trace",
    response_model=PositionTraceRead,
    summary="Post-trade execution trace (signal → close timeline) for a position",
)
async def get_position_trace(
    session: DbSession,
    current_user: CurrentUser,
    position_id: int = Path(ge=1),
) -> PositionTraceRead:
    result = await auto_trade_service.build_position_trace(
        session=session, user_id=current_user.id, position_id=position_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Auto-trade position not found.")
    position, events = result
    return PositionTraceRead(
        position_id=position.id,
        symbol=position.symbol,
        side=position.side,
        status=position.status,
        entry_price=float(position.entry_price),
        close_price=(float(position.close_price) if position.close_price is not None else None),
        close_reason=position.close_reason,
        state=position.state,
        decision_event_id=position.decision_event_id,
        open_history_id=position.open_history_id,
        close_history_id=position.close_history_id,
        open_order_id=position.open_order_id,
        close_order_id=position.close_order_id,
        opened_at=position.opened_at,
        closed_at=position.closed_at,
        events=[AutoTradeEventRead.model_validate(event) for event in events],
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
    config_id: int | None = Query(default=None, ge=1),
) -> AutoTradePositionsResponse:
    try:
        config = await auto_trade_service.get_config(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            config_id=config_id,
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
            config_id=config_id,
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
    config_id: int | None = Query(default=None, ge=1),
) -> AutoTradeLedgerTradesResponse:
    config = await auto_trade_service.get_config(
        session=session,
        user_id=current_user.id,
        account_id=account_id,
        config_id=config_id,
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
        config_id=config_id,
    )
    platform = sum(1 for row in rows if row.origin == "platform")
    external = sum(1 for row in rows if row.origin == "external")
    # Value every fee currency, not just USDT: non-USDT fees (e.g. BNB, the 25%
    # discount case) are converted via best-effort spot marks so total_fee_usdt
    # is not understated. A failed mark fetch degrades to 0 for that asset.
    non_usdt_fee_assets = sorted(
        {(row.fee_currency or "").upper() for row in rows} - {"", "USDT"}
    )
    mark_prices: dict[str, float] = {}
    if non_usdt_fee_assets:
        try:
            mark_prices = await trading_service.fetch_mark_prices(
                session=session,
                user_id=current_user.id,
                account_id=account_id,
                assets=non_usdt_fee_assets,
                quote="USDT",
            )
        except Exception:
            mark_prices = {}
    total_fee = sum_fee_cost_quote(rows, mark_prices)
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
    config_id: int | None = Query(default=None, ge=1),
) -> AutoTradeEventsResponse:
    bounded_limit = max(1, min(limit, 200))
    try:
        rows = await auto_trade_service.list_events(
            session=session,
            user_id=current_user.id,
            limit=bounded_limit,
            account_id=account_id,
            config_id=config_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AutoTradeEventsResponse(
        events=[AutoTradeEventRead.model_validate(item) for item in rows]
    )


# ─────────── W7 multi-strategy partitioning ──────────────────────────────


@router.get(
    "/auto-trade/portfolio",
    response_model=PortfolioSummaryResponse,
    summary="Aggregated portfolio view across all strategies",
    description=(
        "Returns one row per :class:`AutoTradeConfig` owned by the user, "
        "with realized/unrealized PnL, open position count, and live "
        "sub-account balance. Balance fetches run in parallel; one failing "
        "exchange surfaces as ``balance_error`` on that row instead of "
        "failing the whole response."
    ),
)
async def get_auto_trade_portfolio(
    session: DbSession,
    current_user: CurrentUser,
) -> PortfolioSummaryResponse:
    summary = await compute_portfolio(
        session=session,
        auto_trade=auto_trade_service,
        trading=trading_service,
        user_id=current_user.id,
        fetch_balances=True,
    )
    return PortfolioSummaryResponse(
        strategies=[
            StrategyPortfolioEntryRead(
                config_id=entry.config_id,
                account_id=entry.account_id,
                account_label=entry.account_label,
                exchange_name=entry.exchange_name,
                mode=entry.mode,
                lifecycle_stage=entry.lifecycle_stage,
                strategy_name=entry.strategy_name,
                profile_id=entry.profile_id,
                profile_symbol=entry.profile_symbol,
                is_running=entry.is_running,
                enabled=entry.enabled,
                open_positions_count=entry.open_positions_count,
                margin_used_usdt=entry.margin_used_usdt,
                realized_pnl_usdt=entry.realized_pnl_usdt,
                unrealized_pnl_usdt=entry.unrealized_pnl_usdt,
                balance_total_usdt=entry.balance_total_usdt,
                balance_free_usdt=entry.balance_free_usdt,
                last_started_at=entry.last_started_at,
                last_stopped_at=entry.last_stopped_at,
                balance_error=entry.balance_error,
                win_rate_pct=entry.win_rate_pct,
                max_dd_pct=entry.max_dd_pct,
                sharpe_proxy=entry.sharpe_proxy,
                roi_pct=entry.roi_pct,
                health_class=entry.health_class,
                sample_size=entry.sample_size,
                kpi_as_of=entry.kpi_as_of,
            )
            for entry in summary.strategies
        ],
        total_realized_pnl_usdt=summary.total_realized_pnl_usdt,
        total_unrealized_pnl_usdt=summary.total_unrealized_pnl_usdt,
        total_open_positions=summary.total_open_positions,
        total_running_strategies=summary.total_running_strategies,
        portfolio_max_dd_pct=summary.portfolio_max_dd_pct,
    )


@router.post(
    "/auto-trade/play-all",
    response_model=BulkLifecycleResponse,
    summary="Start every enabled strategy for the user",
    description=(
        "Bulk variant of ``/auto-trade/play``. Skips configs that are "
        "disabled or already running. One failure does not abort the "
        "others — every config gets its own per-row outcome."
    ),
)
async def play_all_auto_trade(
    session: DbSession,
    current_user: RequireStepUp,
) -> BulkLifecycleResponse:
    # Review I6: bulk go-live is real-money, gated by step-up like single /play.
    # (stop-all stays ungated — stopping is a frictionless de-risking action.)
    outcome = await auto_trade_service.set_running_bulk(
        session=session,
        user_id=current_user.id,
        is_running=True,
    )
    return BulkLifecycleResponse(
        requested=int(outcome["requested"]),
        succeeded=int(outcome["succeeded"]),
        skipped=int(outcome["skipped"]),
        failed=int(outcome["failed"]),
        results=[BulkLifecycleResultItem(**item) for item in outcome["results"]],
    )


@router.post(
    "/auto-trade/stop-all",
    response_model=BulkLifecycleResponse,
    summary="Stop every running strategy for the user",
    description=(
        "Bulk variant of ``/auto-trade/stop``. Only flips ``is_running=false`` "
        "— open positions are deliberately not closed. Use "
        "``/auto-trade/close-positions`` (per account) when you want to "
        "flatten."
    ),
)
async def stop_all_auto_trade(
    session: DbSession,
    current_user: CurrentUser,
) -> BulkLifecycleResponse:
    outcome = await auto_trade_service.set_running_bulk(
        session=session,
        user_id=current_user.id,
        is_running=False,
    )
    return BulkLifecycleResponse(
        requested=int(outcome["requested"]),
        succeeded=int(outcome["succeeded"]),
        skipped=int(outcome["skipped"]),
        failed=int(outcome["failed"]),
        results=[BulkLifecycleResultItem(**item) for item in outcome["results"]],
    )


@router.get(
    "/auto-trade/balance",
    response_model=AccountBalanceResponse,
    summary="USDT balance for one strategy's sub-account",
    description=(
        "Powers the per-strategy budget card. Returns the free/total USDT "
        "balance fetched live from the exchange. ``error`` non-null means "
        "the fetch failed; the dashboard treats it as 'balance unavailable'."
    ),
)
async def get_auto_trade_balance(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
) -> AccountBalanceResponse:
    try:
        snapshot = await trading_service.get_spot_balances(
            session=session, user_id=current_user.id, account_id=account_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        return AccountBalanceResponse(
            account_id=account_id,
            exchange_name="",
            mode="",
            free_usdt=None,
            total_usdt=None,
            error=str(exc),
        )
    free_total = 0.0
    total_total = 0.0
    for item in snapshot.balances:
        if str(getattr(item, "asset", "")).upper() != "USDT":
            continue
        free_total += float(getattr(item, "free", 0.0) or 0.0)
        total_total += float(getattr(item, "total", 0.0) or 0.0)
    return AccountBalanceResponse(
        account_id=account_id,
        exchange_name=snapshot.exchange_name,
        mode=snapshot.mode,
        free_usdt=free_total,
        total_usdt=total_total,
        error=None,
    )


# ───────────────────────── telegram notifications ──────────────────────────


@router.get(
    "/notifications/telegram",
    response_model=TelegramSettingsOut,
    summary="Get Telegram notification settings",
)
async def get_telegram_settings(
    session: DbSession,
    current_user: CurrentUser,
) -> TelegramSettingsOut:
    view = await telegram_notify_service.get_settings_view(
        session=session, user_id=current_user.id
    )
    return TelegramSettingsOut(**view)


@router.put(
    "/notifications/telegram",
    response_model=TelegramSettingsOut,
    summary="Update Telegram notification settings",
)
async def update_telegram_settings(
    payload: TelegramSettingsUpdate,
    session: DbSession,
    current_user: CurrentUser,
) -> TelegramSettingsOut:
    view = await telegram_notify_service.update_settings(
        session=session,
        user_id=current_user.id,
        enabled=payload.enabled,
        notify_on_open=payload.notify_on_open,
        notify_on_close=payload.notify_on_close,
        notify_on_risk=payload.notify_on_risk,
    )
    return TelegramSettingsOut(**view)


@router.post(
    "/notifications/telegram/link",
    response_model=TelegramLinkOut,
    summary="Generate a one-time Telegram deep link",
)
async def link_telegram(
    session: DbSession,
    current_user: CurrentUser,
) -> TelegramLinkOut:
    if not telegram_notify_service.configured:
        raise HTTPException(status_code=503, detail="Telegram notifications are not configured.")
    link = await telegram_notify_service.generate_link(
        session=session, user_id=current_user.id
    )
    return TelegramLinkOut(
        code=str(link["code"]),
        deep_link=link["deep_link"],
        expires_at=link["expires_at"],
    )


@router.post(
    "/notifications/telegram/test",
    response_model=TelegramTestResult,
    summary="Send a Telegram test notification",
)
async def test_telegram(
    session: DbSession,
    current_user: CurrentUser,
) -> TelegramTestResult:
    result = await telegram_notify_service.send_test_message(
        session=session, user_id=current_user.id
    )
    return TelegramTestResult(status=result.status.value, error=result.error)


@router.delete(
    "/notifications/telegram",
    response_model=TelegramSettingsOut,
    summary="Unlink Telegram notifications",
)
async def unlink_telegram(
    session: DbSession,
    current_user: CurrentUser,
) -> TelegramSettingsOut:
    await telegram_notify_service.unlink(session=session, user_id=current_user.id)
    view = await telegram_notify_service.get_settings_view(
        session=session, user_id=current_user.id
    )
    return TelegramSettingsOut(**view)
