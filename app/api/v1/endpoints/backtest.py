import math
from typing import Any, cast

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from taskiq import AsyncTaskiqTask

from app.api.deps import CurrentUser, DbSession
from app.schemas.audit import AuditLogCreate
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
    AiForecastBacktestFile,
    AiForecastBacktestFilesResponse,
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
    VwapAiComparisonDelta,
    VwapAiComparisonResponse,
    VwapBacktestRequest,
    VwapCatalog,
)
from app.services.backtesting.run_manifest import build_metric_formula_definition
from app.services.backtesting.service import BacktestingService
from app.services.state.audit_service import AuditService
from app.worker.tasks import run_portfolio_backtest

router = APIRouter()
service = BacktestingService()
audit_service = AuditService()


def _strategy_params(model_cls: type[BaseModel]) -> list[str]:
    return sorted(model_cls.model_fields.keys())


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
    "/ai-forecast-files",
    response_model=AiForecastBacktestFilesResponse,
    summary="List available AI forecast backtest CSV files",
)
async def list_ai_forecast_backtest_files() -> AiForecastBacktestFilesResponse:
    files = await service.list_ai_forecast_backtest_files()
    return AiForecastBacktestFilesResponse(
        files=[
            AiForecastBacktestFile(
                file_name=item["file_name"],
                modified_at_utc=item["modified_at_utc"],
            )
            for item in files
        ]
    )


@router.get(
    "/metrics-schema",
    summary="Get metric formula definition + version (Finding 7.3 reproducibility)",
)
async def get_metric_formula_definition(version: str | None = None) -> dict[str, Any]:
    return build_metric_formula_definition(version)


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
            builtin_strategy_params={
                "VWAP Builder": _strategy_params(VwapBacktestRequest),
                "ATR Order-Block": _strategy_params(AtrOrderBlockRequest),
                "Knife Catcher": _strategy_params(KnifeCatcherRequest),
                "Grid BOT": _strategy_params(GridBotRequest),
                "Intraday Momentum": _strategy_params(IntradayMomentumRequest),
            },
        ),
    )


def _truncate_and_strip_series(
    data: dict[str, object],
    include_series: bool,
    trades_limit: int,
) -> None:
    if not include_series:
        data["chart_points"] = {}
    trades = data.get("trades")
    if isinstance(trades, list):
        data["trades"] = trades[:trades_limit]


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return default


@router.post(
    "/vwap",
    response_model=BacktestResponse | VwapAiComparisonResponse,
    summary="Run VWAP builder backtest",
)
async def run_vwap_backtest(
    payload: VwapBacktestRequest,
    session: DbSession,
    current_user: CurrentUser,
) -> BacktestResponse | VwapAiComparisonResponse:
    try:
        if payload.run_with_ai:
            data = await service.run_vwap_with_ai(payload.model_dump())
            baseline = data.get("baseline")
            ai_forecast = data.get("ai_forecast")
            if not isinstance(baseline, dict) or not isinstance(ai_forecast, dict):
                raise ValueError("VWAP AI comparison payload is invalid.")
            _truncate_and_strip_series(
                baseline,
                include_series=False,
                trades_limit=payload.trades_limit,
            )
            _truncate_and_strip_series(
                ai_forecast,
                include_series=payload.include_series,
                trades_limit=payload.trades_limit,
            )
            ai_summary = ai_forecast.get("summary", {})
            baseline_summary = baseline.get("summary", {})
            if not isinstance(ai_summary, dict) or not isinstance(baseline_summary, dict):
                raise ValueError("VWAP AI comparison summary payload is invalid.")
            response_payload: BacktestResponse | VwapAiComparisonResponse = (
                VwapAiComparisonResponse(
                    result=BacktestResponse(**ai_forecast),
                    baseline=BacktestResponse(**baseline),
                    comparison=VwapAiComparisonDelta(
                        total_pnl_delta=_to_float(ai_summary.get("total_pnl"))
                        - _to_float(baseline_summary.get("total_pnl")),
                        win_rate_delta=_to_float(ai_summary.get("win_rate"))
                        - _to_float(baseline_summary.get("win_rate")),
                        trades_delta=_to_int(ai_summary.get("total_trades"))
                        - _to_int(baseline_summary.get("total_trades")),
                        profit_factor_delta=_to_float(ai_summary.get("profit_factor"))
                        - _to_float(baseline_summary.get("profit_factor")),
                        sharpe_proxy_delta=_to_float(ai_summary.get("sharpe_proxy"))
                        - _to_float(baseline_summary.get("sharpe_proxy")),
                        max_drawdown_delta=_to_float(ai_summary.get("max_drawdown"))
                        - _to_float(baseline_summary.get("max_drawdown")),
                        calmar_ratio_delta=_to_float(ai_summary.get("calmar_ratio"))
                        - _to_float(baseline_summary.get("calmar_ratio")),
                    ),
                )
            )
        else:
            data = await service.run_vwap(payload.model_dump())
            if not isinstance(data, dict):
                raise ValueError("VWAP backtest payload is invalid.")
            _truncate_and_strip_series(
                data,
                include_series=payload.include_series,
                trades_limit=payload.trades_limit,
            )
            response_payload = BacktestResponse(**data)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    await audit_service.create_event(
        session=session,
        actor=current_user.email,
        payload=AuditLogCreate(
            event="BUILDER_CHANGE",
            reason="User ran VWAP builder backtest.",
            target_type="backtest",
            target_id="vwap",
            payload={
                "symbol": payload.symbol,
                "timeframe": payload.timeframe,
                "preset": payload.preset,
                "regime": payload.regime,
                "enabled": payload.enabled,
                "run_with_ai": payload.run_with_ai,
                "ai_forecast_file": payload.ai_forecast_file,
                "ai_bull_confidence_threshold": payload.ai_bull_confidence_threshold,
                "ai_bear_confidence_threshold": payload.ai_bear_confidence_threshold,
            },
        ),
    )
    return response_payload


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
async def run_portfolio(
    payload: PortfolioBacktestRequest,
    session: DbSession,
    current_user: CurrentUser,
) -> dict[str, object]:
    request_payload = payload.model_dump()
    request_payload["user_id"] = current_user.id
    if payload.async_job:
        task: AsyncTaskiqTask[dict[str, object]] = await run_portfolio_backtest.kiq(request_payload)
        return {"status": "queued", "task_id": task.task_id}
    request_payload["session"] = session
    try:
        data = await service.run_portfolio(request_payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    await audit_service.create_event(
        session=session,
        actor=current_user.email,
        payload=AuditLogCreate(
            event="PORTFOLIO_RUN",
            reason="User ran portfolio backtest.",
            target_type="portfolio",
            target_id="portfolio",
            payload={
                "total_capital": payload.total_capital,
                "user_strategies_count": len(payload.user_strategies),
                "builtin_strategies_count": len(payload.builtin_strategies),
                "legacy_strategies_count": len(payload.strategies),
            },
        ),
    )
    if payload.run_with_ai:
        return VwapAiComparisonResponse(**data).model_dump()
    return BacktestResponse(**data).model_dump()
