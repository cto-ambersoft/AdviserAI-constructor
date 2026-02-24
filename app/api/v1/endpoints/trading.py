from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.schemas.exchange_trading import (
    SpotBalancesRead,
    SpotOrderCreate,
    SpotOrderRead,
    SpotOrdersRead,
    SpotPnlRead,
    SpotPositionsRead,
    SpotTradesRead,
)
from app.services.execution.errors import ExchangeServiceError, error_http_status
from app.services.execution.trading_service import TradingService

router = APIRouter()
trading_service = TradingService()
settings = get_settings()


@router.post("/spot/orders", response_model=SpotOrderRead, summary="Place spot order")
async def place_spot_order(
    payload: SpotOrderCreate,
    session: DbSession,
    current_user: CurrentUser,
) -> SpotOrderRead:
    try:
        return await trading_service.place_spot_order(
            session=session,
            user_id=current_user.id,
            payload=payload,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc


@router.delete("/spot/orders/{order_id}", response_model=SpotOrderRead, summary="Cancel spot order")
async def cancel_spot_order(
    order_id: str,
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
    symbol: str | None = Query(default=None, min_length=3, max_length=32),
) -> SpotOrderRead:
    try:
        return await trading_service.cancel_spot_order(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            order_id=order_id,
            symbol=symbol,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc


@router.get(
    "/spot/orders/detail/{order_id}",
    response_model=SpotOrderRead,
    summary="Get detailed spot order",
)
async def get_spot_order_detail(
    order_id: str,
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
    symbol: str = Query(min_length=3, max_length=32),
) -> SpotOrderRead:
    try:
        return await trading_service.get_spot_order_detail(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            order_id=order_id,
            symbol=symbol,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc


@router.get("/spot/orders/open", response_model=SpotOrdersRead, summary="List open spot orders")
async def get_open_spot_orders(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
    symbol: str | None = Query(default=None, min_length=3, max_length=32),
    limit: int = Query(default=settings.exchange_default_page_limit, ge=1, le=500),
) -> SpotOrdersRead:
    try:
        return await trading_service.get_spot_open_orders(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            symbol=symbol,
            limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc


@router.get(
    "/spot/orders/history", response_model=SpotOrdersRead, summary="List closed spot orders"
)
async def get_spot_order_history(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
    symbol: str | None = Query(default=None, min_length=3, max_length=32),
    limit: int = Query(default=settings.exchange_default_page_limit, ge=1, le=500),
) -> SpotOrdersRead:
    try:
        return await trading_service.get_spot_order_history(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            symbol=symbol,
            limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc


@router.get("/spot/trades", response_model=SpotTradesRead, summary="List spot trades")
async def get_spot_trades(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
    symbol: str | None = Query(default=None, min_length=3, max_length=32),
    limit: int = Query(default=settings.exchange_default_page_limit, ge=1, le=500),
) -> SpotTradesRead:
    try:
        return await trading_service.get_spot_trades(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            symbol=symbol,
            limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc


@router.get("/spot/balances", response_model=SpotBalancesRead, summary="Get spot balances")
async def get_spot_balances(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
) -> SpotBalancesRead:
    try:
        return await trading_service.get_spot_balances(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc


@router.get("/spot/positions", response_model=SpotPositionsRead, summary="Get spot positions view")
async def get_spot_positions(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
    quote_asset: str = Query(default="USDT", min_length=2, max_length=10),
) -> SpotPositionsRead:
    try:
        return await trading_service.get_spot_positions(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            quote_asset=quote_asset,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc


@router.get("/spot/pnl", response_model=SpotPnlRead, summary="Get spot PnL")
async def get_spot_pnl(
    session: DbSession,
    current_user: CurrentUser,
    account_id: int = Query(ge=1),
    quote_asset: str = Query(default="USDT", min_length=2, max_length=10),
    limit: int = Query(default=settings.exchange_default_page_limit, ge=1, le=1000),
) -> SpotPnlRead:
    try:
        return await trading_service.get_spot_pnl(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            quote_asset=quote_asset,
            limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc
