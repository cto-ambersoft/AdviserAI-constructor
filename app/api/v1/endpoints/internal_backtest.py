import math
from typing import Any, cast

from fastapi import APIRouter, Header, HTTPException, status

from app.core.config import get_settings
from app.schemas.backtest import (
    AiForecastBacktestFile,
    AiForecastBacktestFilesResponse,
    BacktestResponse,
    InternalBacktestCompareRequest,
    VwapAiComparisonDelta,
    VwapAiComparisonResponse,
)
from app.services.backtesting.service import BacktestingService

router = APIRouter()
service = BacktestingService()


def _assert_internal_key(raw_key: str | None) -> None:
    expected = get_settings().internal_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API key is not configured.",
        )
    if raw_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key.",
        )


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


async def _run_strategy(strategy: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = strategy.strip().lower().replace("_", "-").replace(" ", "-")
    if normalized in {"vwap", "vwap-builder"}:
        if payload.get("run_with_ai"):
            comparison = await service.run_vwap_with_ai_rows(payload)
            ai_forecast = comparison.get("ai_forecast")
            if not isinstance(ai_forecast, dict):
                raise ValueError("VWAP AI comparison payload is invalid.")
            return cast(dict[str, Any], ai_forecast)
        return await service.run_vwap(payload)
    if normalized in {"atr", "atr-order-block", "atr-ob"}:
        return await service.run_atr_order_block(payload)
    if normalized in {"knife", "knife-catcher"}:
        return await service.run_knife(payload)
    if normalized in {"grid", "grid-bot"}:
        return await service.run_grid(payload)
    if normalized in {"intraday", "intraday-momentum"}:
        return await service.run_intraday(payload)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Unsupported internal comparison strategy: {strategy}",
    )


@router.get(
    "/backtest/ai-forecast-files",
    response_model=AiForecastBacktestFilesResponse,
    summary="List available AI forecast CSV files for internal callers",
)
async def list_ai_forecast_files_internal(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
) -> AiForecastBacktestFilesResponse:
    _assert_internal_key(x_internal_api_key)
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


@router.post(
    "/backtest/compare",
    response_model=VwapAiComparisonResponse,
    summary="Run an internal AI-vs-baseline backtest comparison",
)
async def compare_internal(
    payload: InternalBacktestCompareRequest,
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
) -> VwapAiComparisonResponse:
    _assert_internal_key(x_internal_api_key)

    request_payload = {
        **payload.algo_config,
        **payload.data_config,
        "exchange_name": payload.exchange_name,
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "bars": payload.bars,
        "start_time": payload.start_time,
        "end_time": payload.end_time,
        "run_with_ai": True,
        "ai_forecast_rows": payload.ai_forecast_rows,
    }
    baseline_payload = dict(request_payload)
    baseline_payload["run_with_ai"] = False
    baseline_payload.pop("ai_forecast_rows", None)
    ai_payload = dict(request_payload)
    ai_payload["run_with_ai"] = True
    ai_payload["ai_forecast_rows"] = payload.ai_forecast_rows

    if payload.strategy.strip().lower() in {"vwap", "vwap_builder", "vwap builder"}:
        data = await service.run_vwap_with_ai_rows(ai_payload)
        baseline = data["baseline"]
        ai_forecast = data["ai_forecast"]
    else:
        baseline = await _run_strategy(payload.strategy, baseline_payload)
        ai_forecast = await _run_strategy(payload.strategy, ai_payload)

    ai_summary = ai_forecast.get("summary", {})
    baseline_summary = baseline.get("summary", {})
    return VwapAiComparisonResponse(
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
