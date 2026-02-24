from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.services.analysis_proxy.service import AnalysisProxyService

router = APIRouter()
analysis_proxy_service = AnalysisProxyService()


@router.post("/trigger-now", summary="Trigger manual market analysis job")
async def trigger_analysis_now() -> Response:
    return await analysis_proxy_service.trigger_now()


@router.get("/runs", summary="Get analysis runs history")
async def get_analysis_runs(
    date: str | None = Query(default=None),
    limit: str | None = Query(default=None),
) -> Response:
    return await analysis_proxy_service.get_runs(date=date, limit=limit)


@router.get("/market-state", summary="Get current market state")
async def get_market_state() -> Response:
    return await analysis_proxy_service.get_market_state()


@router.get("/{symbol}", summary="Get current symbol analysis")
async def get_symbol_analysis(symbol: str) -> Response:
    return await analysis_proxy_service.get_symbol_analysis(symbol=symbol)
