"""Admin proxy for AI backtest endpoints.

Most routes here forward to the core analysis backend (NestJS) using the
internal `X-API-Key` header. The proxy intentionally does not rename or
reshape payloads, so the public API contract is camelCase end-to-end —
matching what core stores in MongoDB and surfaces via Mongoose `lean()`.
A few routes are owned locally (metrics-schema, artifacts) where the data
naturally lives next to the constructor service.
"""

from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from app.api.deps import CurrentAdminUser, RequireStepUp
from app.core.config import get_settings
from app.services.backtesting.artifacts import (
    guess_media_type,
    list_artifacts,
    resolve_artifact_path,
)
from app.services.backtesting.metrics_schema import METRICS_SCHEMA

router = APIRouter()


async def _core_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    settings = get_settings()
    base_url = settings.analysis_backend_base_url.rstrip("/")
    api_key = settings.analysis_backend_api_key
    if not base_url or not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Core analysis backend is not configured.",
        )
    try:
        async with httpx.AsyncClient(timeout=settings.analysis_http_timeout_seconds) as client:
            response = await client.request(
                method,
                f"{base_url}{path}",
                params=params,
                json=json,
                headers={"X-API-Key": api_key},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Core analysis backend is unavailable.",
        ) from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


def _query_params(request: Request) -> dict[str, str]:
    return {key: value for key, value in request.query_params.items()}


@router.get("/ai-configs/schema")
async def get_ai_config_schema() -> Any:
    return await _core_request("GET", "/api/v1/ai-configs/schema")


@router.get("/ai-configs")
async def list_ai_configs(request: Request) -> Any:
    return await _core_request("GET", "/api/v1/ai-configs", params=_query_params(request))


@router.get("/agents")
async def list_agents() -> Any:
    return await _core_request("GET", "/api/v1/agents")


@router.get("/agent-weights")
async def list_agent_weights(request: Request) -> Any:
    return await _core_request("GET", "/api/v1/agent-weights", params=_query_params(request))


@router.get("/ai-forecast-catalogue/metrics-schema")
def get_metrics_schema() -> Any:
    return METRICS_SCHEMA


@router.get("/ai-forecast-catalogue")
async def list_ai_forecast_catalogue(request: Request) -> Any:
    return await _core_request(
        "GET",
        "/api/v1/ai-forecast-catalogue",
        params=_query_params(request),
    )


@router.get("/ai-forecast-catalogue/{forecast_id}")
async def get_ai_forecast_catalogue_entry(forecast_id: str) -> Any:
    return await _core_request("GET", f"/api/v1/ai-forecast-catalogue/{forecast_id}")


@router.post("/ai-forecast-catalogue/rebuild")
async def rebuild_ai_forecast_catalogue(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return await _core_request("POST", "/api/v1/ai-forecast-catalogue/rebuild", json=payload)


@router.get("/backtest-experiments")
async def list_backtest_experiments(request: Request) -> Any:
    return await _core_request("GET", "/api/v1/backtest-experiments", params=_query_params(request))


@router.get("/backtest-experiments/{experiment_id}")
async def get_backtest_experiment(experiment_id: str) -> Any:
    return await _core_request("GET", f"/api/v1/backtest-experiments/{experiment_id}")


@router.post("/backtest-experiments/run")
async def run_backtest_experiment(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return await _core_request("POST", "/api/v1/backtest-experiments/run", json=payload)


@router.get("/ai-decision-events")
async def list_ai_decision_events(request: Request) -> Any:
    return await _core_request("GET", "/api/v1/ai-decision-events", params=_query_params(request))


@router.get("/agent-accuracy")
async def list_agent_accuracy(request: Request) -> Any:
    return await _core_request("GET", "/api/v1/agent-accuracy", params=_query_params(request))


@router.get("/agent-weights/suggestions/{ai_config_id}")
async def suggest_agent_weights(ai_config_id: str) -> Any:
    return await _core_request("GET", f"/api/v1/agent-weights/suggestions/{ai_config_id}")


@router.post("/agent-weights/suggestions/{ai_config_id}/apply")
async def apply_agent_weight_suggestion(
    ai_config_id: str,
    current_user: RequireStepUp,
    _admin: CurrentAdminUser,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> Any:
    # T17 (W12f) + review C2: applying rebinds the live AiConfig weights (T9) and
    # shifts ai_trend (T11) on a SHARED config (no per-user ownership), so it is an
    # ADMIN action gated by a fresh 2FA step-up — not exposed to every trader.
    return await _core_request(
        "POST",
        f"/api/v1/agent-weights/suggestions/{ai_config_id}/apply",
        json=payload,
    )


@router.get("/artifacts")
def list_export_artifacts(prefix: str | None = Query(default=None, max_length=120)) -> Any:
    return {"artifacts": list_artifacts(prefix)}


@router.get("/artifacts/{filename}")
def get_export_artifact(filename: str) -> FileResponse:
    path = resolve_artifact_path(filename)
    return FileResponse(
        path=path,
        media_type=guess_media_type(path),
        filename=path.name,
    )
