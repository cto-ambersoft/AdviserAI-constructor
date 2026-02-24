from fastapi import APIRouter, HTTPException, status
from taskiq import AsyncTaskiqTask

from app.schemas.backtest import (
    ATR_ORDER_BLOCK_TIMEFRAMES,
    GRID_BOT_TIMEFRAMES,
    INTRADAY_MOMENTUM_SIDES,
    INTRADAY_MOMENTUM_TIMEFRAMES,
    KNIFE_CATCHER_ENTRY_MODE_LONG,
    KNIFE_CATCHER_ENTRY_MODE_SHORT,
    KNIFE_CATCHER_SIDES,
    KNIFE_CATCHER_TIMEFRAMES,
    PORTFOLIO_BUILTIN_STRATEGIES,
    PORTFOLIO_TIMEFRAMES,
    VWAP_ALLOWED_INDICATORS,
    VWAP_ALLOWED_PRESETS,
    VWAP_ALLOWED_REGIMES,
    VWAP_STOP_MODES,
    VWAP_TIMEFRAMES,
    AtrOrderBlockCatalog,
    AtrOrderBlockRequest,
    BacktestCatalogResponse,
    BacktestResponse,
    GridBotCatalog,
    GridBotRequest,
    IntradayMomentumCatalog,
    IntradayMomentumRequest,
    KnifeCatcherCatalog,
    KnifeCatcherRequest,
    PortfolioBacktestRequest,
    PortfolioCatalog,
    VwapBacktestRequest,
    VwapCatalog,
)
from app.services.backtesting.service import BacktestingService
from app.worker.tasks import run_portfolio_backtest

router = APIRouter()
service = BacktestingService()


@router.get(
    "/vwap/indicators",
    summary="List available VWAP indicators",
)
async def list_vwap_indicators() -> dict[str, list[str]]:
    return {"indicators": sorted(VWAP_ALLOWED_INDICATORS)}


@router.get(
    "/vwap/presets",
    summary="List available VWAP presets",
)
async def list_vwap_presets() -> dict[str, list[str]]:
    return {"presets": list(VWAP_ALLOWED_PRESETS)}


@router.get(
    "/vwap/regimes",
    summary="List available VWAP market regimes",
)
async def list_vwap_regimes() -> dict[str, list[str]]:
    return {"regimes": list(VWAP_ALLOWED_REGIMES)}


@router.get(
    "/catalog",
    response_model=BacktestCatalogResponse,
    summary="Get backtest metadata catalog",
)
async def get_backtest_catalog() -> BacktestCatalogResponse:
    return BacktestCatalogResponse(
        vwap=VwapCatalog(
            timeframes=list(VWAP_TIMEFRAMES),
            presets=list(VWAP_ALLOWED_PRESETS),
            regimes=list(VWAP_ALLOWED_REGIMES),
            indicators=sorted(VWAP_ALLOWED_INDICATORS),
            stop_modes=list(VWAP_STOP_MODES),
        ),
        atr_order_block=AtrOrderBlockCatalog(timeframes=list(ATR_ORDER_BLOCK_TIMEFRAMES)),
        knife_catcher=KnifeCatcherCatalog(
            timeframes=list(KNIFE_CATCHER_TIMEFRAMES),
            sides=list(KNIFE_CATCHER_SIDES),
            entry_mode_long=list(KNIFE_CATCHER_ENTRY_MODE_LONG),
            entry_mode_short=list(KNIFE_CATCHER_ENTRY_MODE_SHORT),
        ),
        grid_bot=GridBotCatalog(timeframes=list(GRID_BOT_TIMEFRAMES)),
        intraday_momentum=IntradayMomentumCatalog(
            timeframes=list(INTRADAY_MOMENTUM_TIMEFRAMES),
            sides=list(INTRADAY_MOMENTUM_SIDES),
        ),
        portfolio=PortfolioCatalog(
            timeframes=list(PORTFOLIO_TIMEFRAMES),
            builtin_strategies=list(PORTFOLIO_BUILTIN_STRATEGIES),
        ),
    )


@router.post("/vwap", response_model=BacktestResponse, summary="Run VWAP builder backtest")
async def run_vwap_backtest(payload: VwapBacktestRequest) -> BacktestResponse:
    try:
        data = await service.run_vwap(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if not payload.include_series:
        data["chart_points"] = {}
    data["trades"] = data["trades"][: payload.trades_limit]
    return BacktestResponse(**data)


@router.post(
    "/atr-order-block",
    response_model=BacktestResponse,
    summary="Run ATR order-block backtest",
)
async def run_atr_order_block_backtest(payload: AtrOrderBlockRequest) -> BacktestResponse:
    try:
        data = await service.run_atr_order_block(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if not payload.include_series:
        data["chart_points"] = {}
    data["trades"] = data["trades"][: payload.trades_limit]
    return BacktestResponse(**data)


@router.post(
    "/knife-catcher",
    response_model=BacktestResponse,
    summary="Run knife-catcher backtest",
)
async def run_knife_backtest(payload: KnifeCatcherRequest) -> BacktestResponse:
    try:
        data = await service.run_knife(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if not payload.include_series:
        data["chart_points"] = {}
    data["trades"] = data["trades"][: payload.trades_limit]
    return BacktestResponse(**data)


@router.post("/grid-bot", response_model=BacktestResponse, summary="Run grid-bot backtest")
async def run_grid_backtest(payload: GridBotRequest) -> BacktestResponse:
    try:
        data = await service.run_grid(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if not payload.include_series:
        data["chart_points"] = {}
    data["trades"] = data["trades"][: payload.trades_limit]
    return BacktestResponse(**data)


@router.post(
    "/intraday-momentum",
    response_model=BacktestResponse,
    summary="Run intraday momentum backtest",
)
async def run_intraday_backtest(payload: IntradayMomentumRequest) -> BacktestResponse:
    try:
        data = await service.run_intraday(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if not payload.include_series:
        data["chart_points"] = {}
    data["trades"] = data["trades"][: payload.trades_limit]
    return BacktestResponse(**data)


@router.post("/portfolio", summary="Run portfolio backtest")
async def run_portfolio(payload: PortfolioBacktestRequest) -> dict[str, object]:
    if payload.async_job:
        task: AsyncTaskiqTask[dict[str, object]] = await run_portfolio_backtest.kiq(
            payload.model_dump()
        )
        return {"status": "queued", "task_id": task.task_id}
    data = await service.run_portfolio(payload.model_dump())
    return BacktestResponse(**data).model_dump()
