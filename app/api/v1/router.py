from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.v1.endpoints import (
    ai,
    analysis,
    audit,
    auth,
    backtest,
    exchange,
    health,
    live,
    market,
    personal_analysis,
    strategies,
    trading,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])

protected_router = APIRouter(dependencies=[Depends(get_current_user)])
protected_router.include_router(strategies.router, prefix="/strategies", tags=["strategies"])
protected_router.include_router(backtest.router, prefix="/backtest", tags=["backtest"])
protected_router.include_router(exchange.router, prefix="/exchange", tags=["exchange"])
protected_router.include_router(ai.router, prefix="/ai", tags=["ai"])
protected_router.include_router(audit.router, prefix="/audit", tags=["audit"])
protected_router.include_router(market.router, prefix="/market", tags=["market"])
protected_router.include_router(trading.router, prefix="/trading", tags=["trading"])
protected_router.include_router(live.router, prefix="/live", tags=["live"])
protected_router.include_router(analysis.router, prefix="/analysis", tags=["analysis"])
protected_router.include_router(
    personal_analysis.router,
    prefix="/analysis/personal",
    tags=["analysis-personal"],
)

api_router.include_router(protected_router)
