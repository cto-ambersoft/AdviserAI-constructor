from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from sqlalchemy import Select, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_job import PersonalAnalysisJob
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.schemas.personal_analysis import (
    PersonalAnalysisManualTriggerRequest,
    PersonalAnalysisProfileCreate,
    PersonalAnalysisProfileUpdate,
    normalize_agents_and_weights,
)
from app.services.auto_trade.service import AutoTradeService
from app.services.personal_analysis.http_provider import HttpPollingAnalysisProvider
from app.services.personal_analysis.provider import (
    AnalysisProvider,
    AnalysisProviderError,
    CoreJobStatus,
)

JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
_JOB_ACTIVE_STATUSES = (JOB_PENDING, JOB_PROCESSING)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_status(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {JOB_PENDING, JOB_PROCESSING, JOB_COMPLETED, JOB_FAILED}:
        return normalized
    return JOB_PENDING


class PersonalAnalysisService:
    def __init__(self, provider: AnalysisProvider | None = None) -> None:
        self._provider = provider or HttpPollingAnalysisProvider()
        self._auto_trade = AutoTradeService()
        settings = get_settings()
        self._status_batch_size = settings.personal_analysis_status_batch_size
        self._max_attempts = settings.personal_analysis_max_attempts
        self._poll_interval_seconds = settings.personal_analysis_poll_interval_seconds
        self._scheduler_loop_enabled = settings.personal_analysis_scheduler_loop_enabled

    async def list_profiles(
        self,
        *,
        session: AsyncSession,
        user_id: int,
    ) -> list[PersonalAnalysisProfile]:
        rows = await session.scalars(
            select(PersonalAnalysisProfile)
            .where(PersonalAnalysisProfile.user_id == user_id)
            .order_by(PersonalAnalysisProfile.created_at.desc())
        )
        return list(rows.all())

    async def create_profile(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        payload: PersonalAnalysisProfileCreate,
    ) -> PersonalAnalysisProfile:
        now = _utc_now()
        row = PersonalAnalysisProfile(
            user_id=user_id,
            symbol=payload.symbol,
            query_prompt=payload.query_prompt,
            agents=payload.agents or {},
            agent_weights=payload.agent_weights or {},
            interval_minutes=payload.interval_minutes,
            is_active=True,
            next_run_at=now,
            last_triggered_at=None,
            last_completed_at=None,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    async def update_profile(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        profile_id: int,
        payload: PersonalAnalysisProfileUpdate,
    ) -> PersonalAnalysisProfile:
        row = await session.scalar(
            select(PersonalAnalysisProfile).where(
                PersonalAnalysisProfile.id == profile_id,
                PersonalAnalysisProfile.user_id == user_id,
            )
        )
        if row is None:
            raise LookupError("Personal analysis profile not found.")

        updates = payload.model_dump(exclude_none=True)
        if "agents" in updates or "agent_weights" in updates:
            merged_agents = updates.get("agents", row.agents)
            merged_weights = updates.get("agent_weights", row.agent_weights)
            normalized_agents, normalized_weights = normalize_agents_and_weights(
                agents=merged_agents,
                agent_weights=merged_weights,
            )
            row.agents = normalized_agents
            row.agent_weights = normalized_weights

        if "symbol" in updates:
            row.symbol = str(updates["symbol"])
        if "query_prompt" in updates:
            row.query_prompt = str(updates["query_prompt"]) if updates["query_prompt"] else None
        if "interval_minutes" in updates:
            row.interval_minutes = int(updates["interval_minutes"])
        if "is_active" in updates:
            row.is_active = bool(updates["is_active"])
            if row.is_active and row.next_run_at < _utc_now():
                row.next_run_at = _utc_now()
        await session.commit()
        await session.refresh(row)
        return row

    async def deactivate_profile(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        profile_id: int,
    ) -> bool:
        row = await session.scalar(
            select(PersonalAnalysisProfile).where(
                PersonalAnalysisProfile.id == profile_id,
                PersonalAnalysisProfile.user_id == user_id,
            )
        )
        if row is None:
            return False
        row.is_active = False
        await session.commit()
        return True

    async def trigger_profile(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        profile_id: int,
        overrides: PersonalAnalysisManualTriggerRequest | None = None,
    ) -> PersonalAnalysisJob:
        row = await session.scalar(
            select(PersonalAnalysisProfile).where(
                PersonalAnalysisProfile.id == profile_id,
                PersonalAnalysisProfile.user_id == user_id,
            )
        )
        if row is None:
            raise LookupError("Personal analysis profile not found.")

        now = _utc_now()
        payload_json = self._build_payload_for_profile(profile=row, overrides=overrides)
        accepted = await self._provider.request_analysis(payload_json)
        job = PersonalAnalysisJob(
            id=str(uuid4()),
            user_id=user_id,
            profile_id=row.id,
            core_job_id=accepted.job_id,
            status=_normalize_status(accepted.status),
            attempt=1,
            max_attempts=self._max_attempts,
            error=None,
            payload_json=payload_json,
            next_poll_at=now + timedelta(seconds=self._poll_interval_seconds),
            completed_at=None,
            core_deleted_at=None,
        )
        row.last_triggered_at = now
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job

    async def get_job(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        trade_job_id: str,
    ) -> PersonalAnalysisJob | None:
        row = cast(
            PersonalAnalysisJob | None,
            await session.scalar(
                select(PersonalAnalysisJob).where(
                    PersonalAnalysisJob.id == trade_job_id,
                    PersonalAnalysisJob.user_id == user_id,
                )
            ),
        )
        if row is not None:
            return row
        return cast(
            PersonalAnalysisJob | None,
            await session.scalar(
                select(PersonalAnalysisJob).where(
                    PersonalAnalysisJob.core_job_id == trade_job_id,
                    PersonalAnalysisJob.user_id == user_id,
                )
            ),
        )

    async def get_history(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        profile_id: int | None,
        limit: int,
        before: datetime | None,
    ) -> list[PersonalAnalysisHistory]:
        stmt: Select[tuple[PersonalAnalysisHistory]] = select(PersonalAnalysisHistory).where(
            PersonalAnalysisHistory.user_id == user_id
        )
        if profile_id is not None:
            stmt = stmt.where(PersonalAnalysisHistory.profile_id == profile_id)
        if before is not None:
            stmt = stmt.where(PersonalAnalysisHistory.created_at < before)
        stmt = stmt.order_by(PersonalAnalysisHistory.created_at.desc()).limit(limit)
        rows = await session.scalars(stmt)
        return list(rows.all())

    async def get_latest(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        profile_id: int | None,
        symbol: str | None,
    ) -> PersonalAnalysisHistory | None:
        stmt: Select[tuple[PersonalAnalysisHistory]] = select(PersonalAnalysisHistory).where(
            PersonalAnalysisHistory.user_id == user_id
        )
        if profile_id is not None:
            stmt = stmt.where(PersonalAnalysisHistory.profile_id == profile_id)
        if symbol is not None:
            stmt = stmt.where(PersonalAnalysisHistory.symbol == symbol)
        stmt = stmt.order_by(PersonalAnalysisHistory.created_at.desc()).limit(1)
        return cast(PersonalAnalysisHistory | None, await session.scalar(stmt))

    async def dispatch_due_profiles(self, *, session: AsyncSession) -> dict[str, int]:
        if not self._scheduler_loop_enabled:
            return {"triggered": 0, "errors": 0}

        now = _utc_now()
        stats = {"triggered": 0, "errors": 0}

        while True:
            profiles_stmt: Select[tuple[PersonalAnalysisProfile]] = (
                select(PersonalAnalysisProfile)
                .where(
                    PersonalAnalysisProfile.is_active.is_(True),
                    PersonalAnalysisProfile.next_run_at <= now,
                )
                .order_by(PersonalAnalysisProfile.next_run_at.asc())
                .limit(self._status_batch_size)
            )
            profiles_stmt = self._with_for_update_profiles(session=session, stmt=profiles_stmt)
            profiles = list((await session.scalars(profiles_stmt)).all())
            if not profiles:
                break

            for profile in profiles:
                try:
                    payload_json = self._build_payload_for_profile(profile=profile, overrides=None)
                except ValueError:
                    stats["errors"] += 1
                    continue
                try:
                    accepted = await self._provider.request_analysis(payload_json)
                except AnalysisProviderError:
                    stats["errors"] += 1
                    continue

                session.add(
                    PersonalAnalysisJob(
                        id=str(uuid4()),
                        user_id=profile.user_id,
                        profile_id=profile.id,
                        core_job_id=accepted.job_id,
                        status=_normalize_status(accepted.status),
                        attempt=1,
                        max_attempts=self._max_attempts,
                        error=None,
                        payload_json=payload_json,
                        next_poll_at=now + timedelta(seconds=self._poll_interval_seconds),
                        completed_at=None,
                        core_deleted_at=None,
                    )
                )
                profile.last_triggered_at = now
                profile.next_run_at = now + timedelta(minutes=profile.interval_minutes)
                stats["triggered"] += 1

            if len(profiles) < self._status_batch_size:
                break

        await session.commit()
        return stats

    async def poll_pending_jobs(self, *, session: AsyncSession) -> dict[str, int]:
        if not self._scheduler_loop_enabled:
            return {
                "polled": 0,
                "completed": 0,
                "failed": 0,
                "retried": 0,
                "cleanup_pending": 0,
                "errors": 0,
            }

        now = _utc_now()
        stats = {
            "polled": 0,
            "completed": 0,
            "failed": 0,
            "retried": 0,
            "cleanup_pending": 0,
            "errors": 0,
        }
        await self._cleanup_completed_jobs(session=session, now=now, stats=stats)

        while True:
            jobs_stmt: Select[tuple[PersonalAnalysisJob]] = (
                select(PersonalAnalysisJob)
                .where(
                    PersonalAnalysisJob.status.in_(_JOB_ACTIVE_STATUSES),
                    PersonalAnalysisJob.next_poll_at <= now,
                )
                .order_by(PersonalAnalysisJob.next_poll_at.asc())
                .limit(self._status_batch_size)
            )
            jobs_stmt = self._with_for_update_jobs(session=session, stmt=jobs_stmt)
            jobs = list((await session.scalars(jobs_stmt)).all())
            if not jobs:
                break

            stats["polled"] += len(jobs)
            try:
                core_statuses = await self._provider.check_status_batch(
                    [job.core_job_id for job in jobs]
                )
            except AnalysisProviderError as exc:
                for job in jobs:
                    self._mark_transient_error(job=job, error=str(exc), now=now)
                    if job.status == JOB_FAILED:
                        stats["failed"] += 1
                stats["errors"] += 1
                if len(jobs) < self._status_batch_size:
                    break
                continue

            status_by_core_id = {item.job_id: item for item in core_statuses}
            for job in jobs:
                core_status = status_by_core_id.get(job.core_job_id)
                if core_status is None:
                    job.next_poll_at = now + timedelta(seconds=self._poll_interval_seconds)
                    continue

                normalized = _normalize_status(core_status.status)
                if normalized in (JOB_PENDING, JOB_PROCESSING):
                    job.status = normalized
                    job.error = core_status.error
                    job.next_poll_at = now + timedelta(seconds=self._poll_interval_seconds)
                    continue

                if normalized == JOB_COMPLETED:
                    await self._process_completed_job(
                        session=session,
                        job=job,
                        core_status=core_status,
                        now=now,
                        stats=stats,
                    )
                    continue

                await self._process_failed_job(
                    job=job,
                    now=now,
                    stats=stats,
                    error=core_status.error or "Core job failed.",
                )

            if len(jobs) < self._status_batch_size:
                break

        await session.commit()
        return stats

    def _build_payload_for_profile(
        self,
        *,
        profile: PersonalAnalysisProfile,
        overrides: PersonalAnalysisManualTriggerRequest | None,
    ) -> dict[str, object]:
        agents, weights = normalize_agents_and_weights(
            agents=profile.agents,
            agent_weights=profile.agent_weights,
        )
        query_prompt = profile.query_prompt

        if overrides is not None:
            query_prompt = (
                overrides.query_prompt if overrides.query_prompt is not None else query_prompt
            )
            if overrides.agents is not None or overrides.agent_weights is not None:
                merged_agents = overrides.agents if overrides.agents is not None else agents
                merged_weights = (
                    overrides.agent_weights if overrides.agent_weights is not None else weights
                )
                agents, weights = normalize_agents_and_weights(
                    agents=merged_agents,
                    agent_weights=merged_weights,
                )

        payload_json: dict[str, object] = {
            "symbol": profile.symbol,
            "agents": agents,
            "agent_weights": weights,
        }
        if query_prompt:
            payload_json["query_prompt"] = query_prompt
        return payload_json

    async def _process_completed_job(
        self,
        *,
        session: AsyncSession,
        job: PersonalAnalysisJob,
        core_status: CoreJobStatus,
        now: datetime,
        stats: dict[str, int],
    ) -> None:
        try:
            result = await self._provider.fetch_result(job.core_job_id)
        except AnalysisProviderError as exc:
            self._mark_transient_error(job=job, error=str(exc), now=now)
            stats["errors"] += 1
            if job.status == JOB_FAILED:
                stats["failed"] += 1
            return

        if _normalize_status(result.status) != JOB_COMPLETED or result.result_json is None:
            self._mark_transient_error(
                job=job,
                error=result.error or "Core returned completed without result_json.",
                now=now,
            )
            stats["errors"] += 1
            if job.status == JOB_FAILED:
                stats["failed"] += 1
            return

        existing_history = await session.scalar(
            select(PersonalAnalysisHistory).where(PersonalAnalysisHistory.trade_job_id == job.id)
        )
        created_history: PersonalAnalysisHistory | None = None
        if existing_history is None:
            created_history = PersonalAnalysisHistory(
                user_id=job.user_id,
                profile_id=job.profile_id,
                trade_job_id=job.id,
                symbol=str(job.payload_json.get("symbol") or ""),
                analysis_data=result.result_json,
                core_completed_at=result.completed_at,
            )
            session.add(created_history)
            await session.flush()
            await self._auto_trade.enqueue_history_signal(
                session=session,
                history=created_history,
            )

        profile = await session.get(PersonalAnalysisProfile, job.profile_id)
        if profile is not None:
            profile.last_completed_at = result.completed_at or now
        job.status = JOB_COMPLETED
        job.error = None
        job.completed_at = result.completed_at or core_status.completed_at or now
        job.next_poll_at = now + timedelta(seconds=self._poll_interval_seconds)

        try:
            deleted = await self._provider.delete_job(job.core_job_id)
            if deleted:
                job.core_deleted_at = now
            else:
                job.core_deleted_at = None
                stats["cleanup_pending"] += 1
        except AnalysisProviderError:
            job.core_deleted_at = None
            stats["cleanup_pending"] += 1

        stats["completed"] += 1

    async def _process_failed_job(
        self,
        *,
        job: PersonalAnalysisJob,
        now: datetime,
        stats: dict[str, int],
        error: str,
    ) -> None:
        if job.attempt >= job.max_attempts:
            job.status = JOB_FAILED
            job.error = error
            job.completed_at = now
            stats["failed"] += 1
            return

        try:
            accepted = await self._provider.request_analysis(job.payload_json)
        except AnalysisProviderError as exc:
            self._mark_transient_error(job=job, error=str(exc), now=now)
            stats["errors"] += 1
            if job.status == JOB_FAILED:
                stats["failed"] += 1
            return

        job.attempt += 1
        job.core_job_id = accepted.job_id
        job.status = _normalize_status(accepted.status)
        job.error = error
        job.completed_at = None
        job.core_deleted_at = None
        job.next_poll_at = now + timedelta(seconds=self._poll_interval_seconds)
        stats["retried"] += 1

    async def _cleanup_completed_jobs(
        self,
        *,
        session: AsyncSession,
        now: datetime,
        stats: dict[str, int],
    ) -> None:
        cleanup_stmt: Select[tuple[PersonalAnalysisJob]] = (
            select(PersonalAnalysisJob)
            .where(
                PersonalAnalysisJob.status == JOB_COMPLETED,
                PersonalAnalysisJob.core_deleted_at.is_(None),
            )
            .order_by(desc(PersonalAnalysisJob.updated_at))
            .limit(self._status_batch_size)
        )
        cleanup_stmt = self._with_for_update_jobs(session=session, stmt=cleanup_stmt)
        cleanup_jobs = list((await session.scalars(cleanup_stmt)).all())
        for job in cleanup_jobs:
            try:
                deleted = await self._provider.delete_job(job.core_job_id)
                if deleted:
                    job.core_deleted_at = now
                else:
                    stats["cleanup_pending"] += 1
            except AnalysisProviderError:
                stats["cleanup_pending"] += 1

    def _mark_transient_error(self, *, job: PersonalAnalysisJob, error: str, now: datetime) -> None:
        job.error = error
        if job.attempt >= job.max_attempts:
            job.status = JOB_FAILED
            job.completed_at = now
            return
        job.attempt += 1
        if job.attempt >= job.max_attempts:
            job.status = JOB_FAILED
            job.completed_at = now
            return
        job.status = JOB_PENDING
        job.next_poll_at = now + timedelta(seconds=self._poll_interval_seconds)

    def _with_for_update_profiles(
        self,
        *,
        session: AsyncSession,
        stmt: Select[tuple[PersonalAnalysisProfile]],
    ) -> Select[tuple[PersonalAnalysisProfile]]:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            return stmt
        return stmt.with_for_update(skip_locked=True)

    def _with_for_update_jobs(
        self,
        *,
        session: AsyncSession,
        stmt: Select[tuple[PersonalAnalysisJob]],
    ) -> Select[tuple[PersonalAnalysisJob]]:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            return stmt
        return stmt.with_for_update(skip_locked=True)
