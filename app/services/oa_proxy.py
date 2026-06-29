"""Thin client for core's internal Outcome-Aware read API (S7).

Fetches a profile's calibration + accuracy from core so the constructor can serve
it to the per-profile UI panel. Same auth scheme as the other core proxies.
"""

from typing import Any

import httpx
from fastapi import HTTPException, status

from app.core.config import get_settings


class OaProxyClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.analysis_backend_base_url.rstrip("/")
        self._api_key = settings.analysis_backend_api_key
        self._timeout_seconds = settings.analysis_http_timeout_seconds

    async def fetch_calibration(
        self, *, user_id: int, profile_id: int, symbol: str
    ) -> dict[str, Any]:
        headers = {"X-API-Key": self._api_key}
        url = f"{self._base_url}/api/v1/oa-calibration"
        params = {
            "user_id": str(user_id),
            "profile_id": str(profile_id),
            "symbol": symbol,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.get(url, headers=headers, params=params)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Analysis backend is unavailable.",
            ) from exc

        if resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Core OA request failed (status_code={resp.status_code}).",
            )
        body = resp.json()
        if not isinstance(body, dict):
            return {"calibration": None, "accuracy": []}
        return body
