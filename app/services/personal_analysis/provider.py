from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


class AnalysisProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class CoreAcceptedJob:
    job_id: str
    status: str
    created_at: datetime | None
    expires_at: datetime | None


@dataclass(slots=True)
class CoreJobStatus:
    job_id: str
    status: str
    completed_at: datetime | None
    error: str | None
    has_result: bool


@dataclass(slots=True)
class CoreJobResult:
    job_id: str
    status: str
    result_json: dict[str, Any] | None
    completed_at: datetime | None
    error: str | None


class AnalysisProvider(Protocol):
    async def request_analysis(self, payload: dict[str, Any]) -> CoreAcceptedJob:
        raise NotImplementedError

    async def check_status_batch(self, job_ids: list[str]) -> list[CoreJobStatus]:
        raise NotImplementedError

    async def fetch_result(self, job_id: str) -> CoreJobResult:
        raise NotImplementedError

    async def delete_job(self, job_id: str) -> bool:
        raise NotImplementedError
