from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_job import PersonalAnalysisJob
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.services.personal_analysis.provider import CoreAcceptedJob, CoreJobResult, CoreJobStatus
from app.services.personal_analysis.service import PersonalAnalysisService


class _BaseProvider:
    def __init__(self) -> None:
        self.request_calls: list[dict[str, object]] = []
        self.status_calls: list[list[str]] = []
        self.delete_calls: list[str] = []
        self._counter = 0

    async def request_analysis(self, payload: dict[str, object]) -> CoreAcceptedJob:
        self._counter += 1
        self.request_calls.append(payload)
        return CoreAcceptedJob(
            job_id=f"core-job-{self._counter}",
            status="pending",
            created_at=datetime.now(UTC),
            expires_at=None,
        )

    async def delete_job(self, job_id: str) -> bool:
        self.delete_calls.append(job_id)
        return True


class _HappyProvider(_BaseProvider):
    def __init__(self) -> None:
        super().__init__()
        self.result_by_job_id: dict[str, dict[str, object]] = {}

    async def check_status_batch(self, job_ids: list[str]) -> list[CoreJobStatus]:
        self.status_calls.append(job_ids)
        return [
            CoreJobStatus(
                job_id=job_id,
                status="completed",
                completed_at=datetime.now(UTC),
                error=None,
                has_result=True,
            )
            for job_id in job_ids
        ]

    async def fetch_result(self, job_id: str) -> CoreJobResult:
        result = self.result_by_job_id.get(job_id, {"analysisReport": "ok"})
        return CoreJobResult(
            job_id=job_id,
            status="completed",
            result_json=result,
            completed_at=datetime.now(UTC),
            error=None,
        )


class _AlwaysFailProvider(_BaseProvider):
    async def check_status_batch(self, job_ids: list[str]) -> list[CoreJobStatus]:
        self.status_calls.append(job_ids)
        return [
            CoreJobStatus(
                job_id=job_id,
                status="failed",
                completed_at=datetime.now(UTC),
                error="failed in core",
                has_result=False,
            )
            for job_id in job_ids
        ]

    async def fetch_result(self, job_id: str) -> CoreJobResult:
        raise AssertionError("fetch_result should not be called for failed jobs")


class _PendingChunkProvider(_BaseProvider):
    async def check_status_batch(self, job_ids: list[str]) -> list[CoreJobStatus]:
        self.status_calls.append(job_ids)
        return [
            CoreJobStatus(
                job_id=job_id,
                status="pending",
                completed_at=None,
                error=None,
                has_result=False,
            )
            for job_id in job_ids
        ]

    async def fetch_result(self, job_id: str) -> CoreJobResult:
        raise AssertionError("fetch_result should not be called for pending jobs")


@pytest.fixture
async def personal_service_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "personal_analysis_service.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _create_profile(session: AsyncSession, user_id: int = 1) -> PersonalAnalysisProfile:
    profile = PersonalAnalysisProfile(
        user_id=user_id,
        symbol="BTCUSDT",
        query_prompt=None,
        agents={"twitterSentiment": True},
        agent_weights={"twitterSentiment": 1.0},
        interval_minutes=60,
        is_active=True,
        next_run_at=datetime.now(UTC),
        last_triggered_at=None,
        last_completed_at=None,
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def test_poll_happy_path_persists_history_and_cleans_core(
    personal_service_db: async_sessionmaker[AsyncSession],
) -> None:
    provider = _HappyProvider()
    service = PersonalAnalysisService(provider=provider)
    async with personal_service_db() as session:
        profile = await _create_profile(session)
        job = await service.trigger_profile(
            session=session,
            user_id=profile.user_id,
            profile_id=profile.id,
        )
        job.next_poll_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        stats = await service.poll_pending_jobs(session=session)
        assert stats["completed"] == 1
        assert provider.delete_calls == [job.core_job_id]

        refreshed_job = await session.get(PersonalAnalysisJob, job.id)
        assert refreshed_job is not None
        assert refreshed_job.status == "completed"
        assert refreshed_job.core_deleted_at is not None

        history_rows = list(
            (
                await session.scalars(
                    select(PersonalAnalysisHistory).where(
                        PersonalAnalysisHistory.trade_job_id == job.id
                    )
                )
            ).all()
        )
        assert len(history_rows) == 1
        assert history_rows[0].analysis_data["analysisReport"] == "ok"


async def test_poll_failed_jobs_retries_and_then_marks_failed(
    personal_service_db: async_sessionmaker[AsyncSession],
) -> None:
    provider = _AlwaysFailProvider()
    service = PersonalAnalysisService(provider=provider)
    async with personal_service_db() as session:
        profile = await _create_profile(session)
        job = await service.trigger_profile(
            session=session,
            user_id=profile.user_id,
            profile_id=profile.id,
        )

        for _ in range(3):
            current = await session.get(PersonalAnalysisJob, job.id)
            assert current is not None
            current.next_poll_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            await service.poll_pending_jobs(session=session)

        final_job = await session.get(PersonalAnalysisJob, job.id)
        assert final_job is not None
        assert final_job.status == "failed"
        assert final_job.completed_at is not None
        assert final_job.attempt == final_job.max_attempts


async def test_poll_uses_batch_chunking_for_large_pending_queue(
    personal_service_db: async_sessionmaker[AsyncSession],
) -> None:
    provider = _PendingChunkProvider()
    service = PersonalAnalysisService(provider=provider)
    now = datetime.now(UTC)
    async with personal_service_db() as session:
        profile = await _create_profile(session)
        for idx in range(250):
            session.add(
                PersonalAnalysisJob(
                    id=f"job-{idx}",
                    user_id=profile.user_id,
                    profile_id=profile.id,
                    core_job_id=f"core-job-{idx}",
                    status="pending",
                    attempt=1,
                    max_attempts=3,
                    error=None,
                    payload_json={"symbol": "BTCUSDT"},
                    next_poll_at=now - timedelta(seconds=5),
                    completed_at=None,
                    core_deleted_at=None,
                )
            )
        await session.commit()

        stats = await service.poll_pending_jobs(session=session)
        assert stats["polled"] == 250
        assert len(provider.status_calls) == 3
        assert [len(call) for call in provider.status_calls] == [100, 100, 50]


async def test_dispatch_triggers_only_due_active_profiles(
    personal_service_db: async_sessionmaker[AsyncSession],
) -> None:
    provider = _PendingChunkProvider()
    service = PersonalAnalysisService(provider=provider)
    now = datetime.now(UTC)
    async with personal_service_db() as session:
        active_due = PersonalAnalysisProfile(
            user_id=1,
            symbol="BTCUSDT",
            query_prompt=None,
            agents={"twitterSentiment": True},
            agent_weights={"twitterSentiment": 1.0},
            interval_minutes=60,
            is_active=True,
            next_run_at=now - timedelta(minutes=1),
            last_triggered_at=None,
            last_completed_at=None,
        )
        inactive_due = PersonalAnalysisProfile(
            user_id=1,
            symbol="ETHUSDT",
            query_prompt=None,
            agents={"twitterSentiment": True},
            agent_weights={"twitterSentiment": 1.0},
            interval_minutes=60,
            is_active=False,
            next_run_at=now - timedelta(minutes=1),
            last_triggered_at=None,
            last_completed_at=None,
        )
        session.add(active_due)
        session.add(inactive_due)
        await session.commit()

        stats = await service.dispatch_due_profiles(session=session)
        assert stats["triggered"] == 1
        assert len(provider.request_calls) == 1
        assert provider.request_calls[0]["symbol"] == "BTCUSDT"
