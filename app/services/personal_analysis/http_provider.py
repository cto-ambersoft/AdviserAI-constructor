from datetime import datetime
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.personal_analysis.provider import (
    AnalysisProvider,
    AnalysisProviderError,
    CoreAcceptedJob,
    CoreJobResult,
    CoreJobStatus,
)


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class HttpPollingAnalysisProvider(AnalysisProvider):
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.analysis_backend_base_url.rstrip("/")
        self._api_key = settings.analysis_backend_api_key
        self._timeout_seconds = settings.analysis_http_timeout_seconds

    async def request_analysis(self, payload: dict[str, Any]) -> CoreAcceptedJob:
        body = await self._request_json(
            method="POST",
            path="/api/v1/personal-analysis/jobs",
            json=payload,
        )
        if not isinstance(body, dict):
            raise AnalysisProviderError(
                "Invalid response from Core: expected object.",
                retryable=True,
            )
        job_id = str(body.get("job_id") or "")
        status = str(body.get("status") or "pending")
        if not job_id:
            raise AnalysisProviderError(
                "Invalid response from Core: missing job_id.",
                retryable=True,
            )
        raw_created_at = body.get("created_at") if isinstance(body.get("created_at"), str) else None
        raw_expires_at = body.get("expires_at") if isinstance(body.get("expires_at"), str) else None
        return CoreAcceptedJob(
            job_id=job_id,
            status=status,
            created_at=_parse_dt(raw_created_at),
            expires_at=_parse_dt(raw_expires_at),
        )

    async def check_status_batch(self, job_ids: list[str]) -> list[CoreJobStatus]:
        if not job_ids:
            return []
        body = await self._request_json(
            method="GET",
            path="/api/v1/personal-analysis/jobs/status",
            params={"ids": ",".join(job_ids)},
        )
        if not isinstance(body, dict):
            raise AnalysisProviderError(
                "Invalid response from Core: expected object.",
                retryable=True,
            )
        raw_jobs = body.get("jobs")
        if not isinstance(raw_jobs, list):
            raise AnalysisProviderError(
                "Invalid response from Core: jobs must be a list.",
                retryable=True,
            )

        parsed: list[CoreJobStatus] = []
        for item in raw_jobs:
            if not isinstance(item, dict):
                continue
            raw_completed_at = (
                item.get("completed_at") if isinstance(item.get("completed_at"), str) else None
            )
            parsed.append(
                CoreJobStatus(
                    job_id=str(item.get("job_id") or ""),
                    status=str(item.get("status") or "pending"),
                    completed_at=_parse_dt(raw_completed_at),
                    error=str(item.get("error")) if item.get("error") is not None else None,
                    has_result=bool(item.get("has_result")),
                )
            )
        return [job for job in parsed if job.job_id]

    async def fetch_result(self, job_id: str) -> CoreJobResult:
        body = await self._request_json(
            method="GET",
            path=f"/api/v1/personal-analysis/jobs/{job_id}",
        )
        if not isinstance(body, dict):
            raise AnalysisProviderError(
                "Invalid response from Core: expected object.",
                retryable=True,
            )
        raw_result = body.get("result_json")
        result_json = raw_result if isinstance(raw_result, dict) else None
        raw_completed_at = (
            body.get("completed_at") if isinstance(body.get("completed_at"), str) else None
        )
        return CoreJobResult(
            job_id=str(body.get("job_id") or job_id),
            status=str(body.get("status") or "pending"),
            result_json=result_json,
            completed_at=_parse_dt(raw_completed_at),
            error=str(body.get("error")) if body.get("error") is not None else None,
        )

    async def delete_job(self, job_id: str) -> bool:
        try:
            body = await self._request_json(
                method="DELETE",
                path=f"/api/v1/personal-analysis/jobs/{job_id}",
            )
        except AnalysisProviderError as exc:
            if "status_code=404" in str(exc):
                return True
            raise
        if not isinstance(body, dict):
            return False
        return bool(body.get("deleted", False))

    async def _request_json(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"X-API-Key": self._api_key}
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json,
                )
        except httpx.RequestError as exc:
            raise AnalysisProviderError(
                "Core analysis backend is unavailable.",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            retryable = response.status_code >= 500 or response.status_code in (408, 409, 425, 429)
            detail = response.text.strip() or "error"
            raise AnalysisProviderError(
                f"Core request failed (status_code={response.status_code}): {detail}",
                retryable=retryable,
            )

        if "application/json" not in response.headers.get("content-type", "").lower():
            raise AnalysisProviderError("Core response is not JSON.", retryable=True)
        return response.json()
