from typing import Any

import httpx
from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from app.core.config import get_settings


class AnalysisProxyService:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.analysis_backend_base_url.rstrip("/")
        self._api_key = settings.analysis_backend_api_key
        self._timeout_seconds = settings.analysis_http_timeout_seconds

    async def trigger_now(self) -> Response:
        return await self._request("POST", "/api/analysis/trigger-now")

    async def get_runs(self, date: str | None, limit: str | None) -> Response:
        params: dict[str, str] = {}
        if date is not None:
            params["date"] = date
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", "/api/analysis/runs", params=params, normalize_runs=True)

    async def get_market_state(self) -> Response:
        return await self._request("GET", "/api/analysis/market-state")

    async def get_symbol_analysis(self, symbol: str) -> Response:
        return await self._request("GET", f"/api/analysis/{symbol}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        normalize_runs: bool = False,
    ) -> Response:
        headers = {"X-API-Key": self._api_key}
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.request(method=method, url=url, headers=headers, params=params)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Analysis backend is unavailable.",
            ) from exc

        content_type = resp.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            payload: Any = resp.json()
            if normalize_runs:
                payload = self._normalize_runs_payload(payload)
            return JSONResponse(content=payload, status_code=resp.status_code)
        return Response(content=resp.content, status_code=resp.status_code, media_type=content_type)

    @staticmethod
    def _normalize_runs_payload(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        runs = payload.get("runs")
        if not isinstance(runs, list):
            return payload

        normalized_runs: list[Any] = []
        for run in runs:
            if not isinstance(run, dict):
                normalized_runs.append(run)
                continue
            normalized = dict(run)
            if "indicatorRecommendations" not in normalized:
                normalized["indicatorRecommendations"] = None
            elif normalized["indicatorRecommendations"] is not None and not isinstance(
                normalized["indicatorRecommendations"], dict
            ):
                normalized["indicatorRecommendations"] = None
            normalized_runs.append(normalized)
        normalized_payload = dict(payload)
        normalized_payload["runs"] = normalized_runs
        return normalized_payload
