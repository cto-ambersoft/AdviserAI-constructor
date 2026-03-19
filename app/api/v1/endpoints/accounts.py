from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.schemas.exchange_trading import AccountTradesRead
from app.services.execution.account_trades_service import AccountTradesService

router = APIRouter()
settings = get_settings()
account_trades_service = AccountTradesService()


@router.get(
    "/{account_id}/trades",
    response_model=AccountTradesRead,
    summary="Get account trades with on-demand sync and PnL",
)
async def get_account_trades(
    account_id: int,
    session: DbSession,
    current_user: CurrentUser,
    symbol: str = Query(min_length=3, max_length=64),
    limit: int = Query(default=settings.exchange_default_page_limit, ge=1, le=1000),
    events_limit: int = Query(default=50, ge=1, le=200),
) -> AccountTradesRead:
    try:
        return await account_trades_service.get_account_trades(
            session=session,
            user_id=current_user.id,
            account_id=account_id,
            symbol=symbol,
            limit=limit,
            events_limit=events_limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
