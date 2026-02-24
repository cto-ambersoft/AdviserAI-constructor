from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, DbSession
from app.schemas.exchange_trading import SpotOrderCreate
from app.schemas.live import (
    AtrObSignalRunRequest,
    BuilderSignalRunRequest,
    LiveSignalResult,
    SignalExecuteRequest,
)
from app.services.execution.errors import ExchangeServiceError, error_http_status
from app.services.execution.trading_service import TradingService
from app.services.live_signals import LiveSignalService

router = APIRouter()
signal_service = LiveSignalService()
trading_service = TradingService()


def _paper_execution(
    *,
    side: str,
    symbol: str,
    entry_price: float,
    entry_usdt: float,
    fee_pct: float,
) -> dict[str, float | str]:
    qty = entry_usdt / entry_price if entry_price > 0 else 0.0
    notional = qty * entry_price
    fee = notional * (fee_pct / 100.0)
    return {
        "mode": "paper",
        "status": "filled",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": entry_price,
        "notional": notional,
        "fee_usdt": fee,
    }


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
    entry_usdt = float(execution.entry_usdt or position_value)
    if entry_usdt <= 0:
        return {"mode": execution.mode, "status": "skipped", "reason": "invalid_entry_usdt"}
    if execution.mode == "paper":
        return _paper_execution(
            side=side,
            symbol=signal_symbol,
            entry_price=entry,
            entry_usdt=entry_usdt,
            fee_pct=float(execution.fee_pct),
        )
    if execution.mode == "live":
        if execution.account_id is None:
            raise HTTPException(status_code=422, detail="account_id is required for live execution")
        payload = SpotOrderCreate(
            account_id=execution.account_id,
            symbol=signal_symbol,
            side="buy" if side == "LONG" else "sell",
            order_type="market",
            amount=entry_usdt / entry,
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
