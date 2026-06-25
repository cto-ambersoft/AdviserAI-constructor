"""Tests for the Data Freshness layer (W8).

T3.1 covers the shared age/freshness helper and the agent_freshness_status
model. The 4h sweep service + cron land in T3.2, the endpoint in T3.3.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.freshness import age_minutes, is_fresh, normalize_to_utc
from app.models.agent_freshness_status import AgentFreshnessStatus
from app.models.auto_trade_event import AutoTradeEvent
from app.models.base import Base
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_job import PersonalAnalysisJob
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.services.personal_analysis.freshness import (
    _upsert_status,
    should_block_stale_entry,
    sweep_agent_freshness,
)

_NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


def test_normalize_to_utc_treats_naive_as_utc() -> None:
    naive = datetime(2026, 5, 28, 11, 30)
    assert normalize_to_utc(naive) == datetime(2026, 5, 28, 11, 30, tzinfo=UTC)
    assert normalize_to_utc(None) is None
    aware = datetime(2026, 5, 28, 11, 30, tzinfo=UTC)
    assert normalize_to_utc(aware) is aware


def test_age_minutes() -> None:
    assert age_minutes(None, now=_NOW) is None
    assert age_minutes(_NOW - timedelta(minutes=30), now=_NOW) == pytest.approx(30.0)
    # naive reference is treated as UTC
    assert age_minutes(datetime(2026, 5, 28, 11, 30), now=_NOW) == pytest.approx(30.0)


def test_is_fresh_boundary_is_inclusive() -> None:
    assert is_fresh(_NOW - timedelta(minutes=240), max_age_minutes=240, now=_NOW) is True
    assert is_fresh(_NOW - timedelta(minutes=241), max_age_minutes=240, now=_NOW) is False
    assert is_fresh(None, max_age_minutes=240, now=_NOW) is False


@pytest.fixture
async def freshness_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'freshness.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_active_profile(
    session: AsyncSession,
    *,
    agents: dict[str, bool] | None = None,
    is_active: bool = True,
    email: str = "freshness@example.com",
) -> PersonalAnalysisProfile:
    user = User(email=email, hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()
    profile = PersonalAnalysisProfile(
        user_id=user.id,
        symbol="BTCUSDT",
        query_prompt=None,
        agents=agents if agents is not None else {"twitterSentiment": True},
        agent_weights={"twitterSentiment": 1.0},
        interval_minutes=60,
        is_active=is_active,
        next_run_at=datetime.now(UTC),
        last_triggered_at=None,
        last_completed_at=None,
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def _seed_profile(session: AsyncSession) -> int:
    profile = await _seed_active_profile(session)
    return profile.id


async def _seed_history(
    session: AsyncSession,
    *,
    profile: PersonalAnalysisProfile,
    core_completed_at: datetime,
    idx: int = 0,
) -> None:
    now = datetime.now(UTC)
    job = PersonalAnalysisJob(
        id=f"job-fresh-{profile.id}-{idx}",
        user_id=profile.user_id,
        profile_id=profile.id,
        core_job_id=f"core-fresh-{profile.id}-{idx}",
        status="completed",
        attempt=1,
        max_attempts=3,
        error=None,
        payload_json={},
        next_poll_at=now,
        completed_at=now,
    )
    session.add(job)
    await session.flush()
    session.add(
        PersonalAnalysisHistory(
            user_id=profile.user_id,
            profile_id=profile.id,
            trade_job_id=job.id,
            symbol=profile.symbol,
            analysis_data={},
            core_completed_at=core_completed_at,
        )
    )
    await session.commit()


async def test_agent_freshness_status_roundtrip(
    freshness_db: async_sessionmaker[AsyncSession],
) -> None:
    async with freshness_db() as session:
        profile_id = await _seed_profile(session)
        row = AgentFreshnessStatus(
            profile_id=profile_id,
            symbol="BTCUSDT",
            agent_key="TW",
            last_data_at=_NOW - timedelta(minutes=10),
            age_minutes=10,
            is_fresh=True,
            checked_at=_NOW,
        )
        session.add(row)
        await session.commit()

        fetched = await session.scalar(
            select(AgentFreshnessStatus).where(AgentFreshnessStatus.profile_id == profile_id)
        )
        assert fetched is not None
        assert fetched.agent_key == "TW"
        assert fetched.is_fresh is True
        assert fetched.age_minutes == 10


async def test_agent_freshness_status_unique_per_profile_agent(
    freshness_db: async_sessionmaker[AsyncSession],
) -> None:
    async with freshness_db() as session:
        profile_id = await _seed_profile(session)
        session.add(
            AgentFreshnessStatus(
                profile_id=profile_id,
                symbol="BTCUSDT",
                agent_key="TW",
                last_data_at=None,
                age_minutes=None,
                is_fresh=False,
                checked_at=_NOW,
            )
        )
        await session.commit()
        # Same (profile_id, agent_key) again ⇒ unique violation.
        session.add(
            AgentFreshnessStatus(
                profile_id=profile_id,
                symbol="BTCUSDT",
                agent_key="TW",
                last_data_at=None,
                age_minutes=None,
                is_fresh=False,
                checked_at=_NOW,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_sweep_marks_fresh_without_event(
    freshness_db: async_sessionmaker[AsyncSession],
) -> None:
    async with freshness_db() as session:
        profile = await _seed_active_profile(session, agents={"twitterSentiment": True})
        now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        await _seed_history(session, profile=profile, core_completed_at=now - timedelta(minutes=10))

        stats = await sweep_agent_freshness(session=session, threshold_minutes=240, now=now)
        assert stats["profiles"] == 1

        rows = (
            await session.scalars(
                select(AgentFreshnessStatus).where(AgentFreshnessStatus.profile_id == profile.id)
            )
        ).all()
        assert {r.agent_key for r in rows} == {"__profile__", "twitterSentiment"}
        assert all(r.is_fresh for r in rows)
        assert all(r.age_minutes == 10 for r in rows)

        events = (
            await session.scalars(
                select(AutoTradeEvent).where(AutoTradeEvent.event_type == "data_stale")
            )
        ).all()
        assert events == []


async def test_sweep_marks_stale_and_emits_event(
    freshness_db: async_sessionmaker[AsyncSession],
) -> None:
    async with freshness_db() as session:
        profile = await _seed_active_profile(session, agents={"twitterSentiment": True})
        now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        await _seed_history(
            session, profile=profile, core_completed_at=now - timedelta(minutes=300)
        )

        await sweep_agent_freshness(session=session, threshold_minutes=240, now=now)

        rows = (
            await session.scalars(
                select(AgentFreshnessStatus).where(AgentFreshnessStatus.profile_id == profile.id)
            )
        ).all()
        assert all(r.is_fresh is False for r in rows)

        event = await session.scalar(
            select(AutoTradeEvent).where(AutoTradeEvent.event_type == "data_stale")
        )
        assert event is not None
        assert event.user_id == profile.user_id
        assert event.profile_id == profile.id
        assert event.payload["symbol"] == "BTCUSDT"
        assert event.payload["age_minutes"] == 300


async def test_sweep_no_history(freshness_db: async_sessionmaker[AsyncSession]) -> None:
    async with freshness_db() as session:
        profile = await _seed_active_profile(session, agents={"twitterSentiment": True})
        now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)

        await sweep_agent_freshness(session=session, threshold_minutes=240, now=now)

        row = await session.scalar(
            select(AgentFreshnessStatus).where(
                AgentFreshnessStatus.profile_id == profile.id,
                AgentFreshnessStatus.agent_key == "__profile__",
            )
        )
        assert row is not None
        assert row.last_data_at is None
        assert row.age_minutes is None
        assert row.is_fresh is False


async def test_sweep_per_agent_only_enabled(
    freshness_db: async_sessionmaker[AsyncSession],
) -> None:
    async with freshness_db() as session:
        profile = await _seed_active_profile(
            session,
            agents={"twitterSentiment": True, "news": False, "techModel": True},
        )
        now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        await _seed_history(session, profile=profile, core_completed_at=now - timedelta(minutes=5))

        await sweep_agent_freshness(session=session, threshold_minutes=240, now=now)

        rows = (
            await session.scalars(
                select(AgentFreshnessStatus).where(AgentFreshnessStatus.profile_id == profile.id)
            )
        ).all()
        assert {r.agent_key for r in rows} == {"__profile__", "twitterSentiment", "techModel"}


async def test_sweep_is_idempotent_upsert(
    freshness_db: async_sessionmaker[AsyncSession],
) -> None:
    async with freshness_db() as session:
        profile = await _seed_active_profile(session, agents={"twitterSentiment": True})
        now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        await _seed_history(session, profile=profile, core_completed_at=now - timedelta(minutes=10))

        await sweep_agent_freshness(session=session, threshold_minutes=240, now=now)
        await sweep_agent_freshness(session=session, threshold_minutes=240, now=now)

        rows = (
            await session.scalars(
                select(AgentFreshnessStatus).where(AgentFreshnessStatus.profile_id == profile.id)
            )
        ).all()
        # Still one row per (profile, agent), not duplicated.
        assert len(rows) == 2


def test_freshness_sweep_cron_registered() -> None:
    from app.worker import tasks as worker_tasks

    schedule = worker_tasks.sweep_agent_data_freshness.labels["schedule"]
    assert any(entry.get("cron") == "0 */4 * * *" for entry in schedule)
    assert any(entry.get("schedule_id") == "agent_freshness_every_4h" for entry in schedule)


async def test_upsert_status_updates_existing_row_no_duplicate(
    freshness_db: async_sessionmaker[AsyncSession],
) -> None:
    """I7 — _upsert_status is an atomic upsert: a second call updates, never duplicates."""

    async with freshness_db() as session:
        profile = await _seed_active_profile(session)
        await _upsert_status(
            session,
            profile_id=profile.id,
            symbol="BTCUSDT",
            agent_key="TW",
            last_data_at=None,
            age_min=300,
            fresh=False,
            checked_at=_NOW,
        )
        await session.commit()
        await _upsert_status(
            session,
            profile_id=profile.id,
            symbol="BTCUSDT",
            agent_key="TW",
            last_data_at=_NOW,
            age_min=5,
            fresh=True,
            checked_at=_NOW,
        )
        await session.commit()

        rows = (
            await session.scalars(
                select(AgentFreshnessStatus).where(AgentFreshnessStatus.profile_id == profile.id)
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].is_fresh is True
        assert rows[0].age_minutes == 5


# --- T14 (W8b): pre-trade staleness gate (acts on staleness) ---


def test_should_block_stale_entry_blocks_when_enabled_and_stale() -> None:
    stale = _NOW - timedelta(minutes=300)
    assert (
        should_block_stale_entry(
            reference_at=stale, now=_NOW, threshold_minutes=240, enabled=True
        )
        is True
    )


def test_should_block_stale_entry_allows_when_fresh() -> None:
    fresh = _NOW - timedelta(minutes=10)
    assert (
        should_block_stale_entry(
            reference_at=fresh, now=_NOW, threshold_minutes=240, enabled=True
        )
        is False
    )


def test_should_block_stale_entry_disabled_never_blocks() -> None:
    # Safe default: off → never blocks (no behavior change until opted in).
    stale = _NOW - timedelta(minutes=999)
    assert (
        should_block_stale_entry(
            reference_at=stale, now=_NOW, threshold_minutes=240, enabled=False
        )
        is False
    )


def test_should_block_stale_entry_blocks_when_no_data() -> None:
    assert (
        should_block_stale_entry(
            reference_at=None, now=_NOW, threshold_minutes=240, enabled=True
        )
        is True
    )
