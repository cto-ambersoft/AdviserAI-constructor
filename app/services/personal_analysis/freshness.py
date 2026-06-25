"""Scheduled AI data-freshness sweep (W8 — T3.2).

Every 4h (cron in ``app.worker.tasks``) this checks, per active analysis
profile and per enabled AI agent, how stale the underlying data is and upserts
an :class:`AgentFreshnessStatus` row per ``(profile_id, agent_key)``. A stale
profile also emits one ``data_stale`` :class:`AutoTradeEvent` so the W12
alerting/UI surface can react.

Acting on staleness (T14/W8b): :func:`should_block_stale_entry` lets the pre-trade
path BLOCK a new entry when the strategy's latest analysis is stale (behind
``agent_freshness_block_enabled``, off by default), not just alert.

Per-agent freshness is derived in the constructor from the profile's latest
``PersonalAnalysisHistory`` — so the enabled agents currently share the profile's
data recency. True per-agent timestamps live in the core service
(``AiDecisionEvent.perAgent``); surfacing them to the constructor is cross-service
work deferred to a follow-up (tracked in tasks/m4-remediation-todo.md).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.freshness import age_minutes, is_fresh, normalize_to_utc
from app.models.agent_freshness_status import AgentFreshnessStatus
from app.models.auto_trade_event import AutoTradeEvent
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_profile import PersonalAnalysisProfile

DEFAULT_FRESHNESS_THRESHOLD_MINUTES = 240

# Columns refreshed on an upsert conflict (everything except the conflict key).
_UPSERT_SET_COLUMNS = ("symbol", "last_data_at", "age_minutes", "is_fresh", "checked_at")

# Synthetic agent key for the per-profile aggregate row (alongside the
# per-enabled-agent rows).
PROFILE_AGENT_KEY = "__profile__"

_EVENT_TYPE_DATA_STALE = "data_stale"
_EVENT_LEVEL_WARNING = "warning"


def should_block_stale_entry(
    *,
    reference_at: datetime | None,
    now: datetime,
    threshold_minutes: int,
    enabled: bool,
) -> bool:
    """T14 (W8b): whether a NEW entry must be blocked because the strategy's latest
    AI analysis is stale (acting on staleness, not just alerting).

    ``enabled=False`` (the safe default) never blocks — the 4h sweep still alerts.
    Missing data (``reference_at is None``) is treated as stale, so a strategy with
    no fresh analysis does not trade when the gate is on.
    """
    if not enabled:
        return False
    return not is_fresh(reference_at, max_age_minutes=threshold_minutes, now=now)


async def _upsert_status(
    session: AsyncSession,
    *,
    profile_id: int,
    symbol: str,
    agent_key: str,
    last_data_at: datetime | None,
    age_min: int | None,
    fresh: bool,
    checked_at: datetime,
) -> None:
    """Atomic upsert of one (profile, agent) freshness row.

    Uses ``INSERT … ON CONFLICT DO UPDATE`` on the ``(profile_id, agent_key)``
    unique constraint rather than select-then-insert, so two overlapping sweeps
    cannot race into an ``IntegrityError`` that aborts the whole commit
    (review I7). Dialect-aware (PostgreSQL in prod, SQLite in tests).
    """
    values = {
        "profile_id": profile_id,
        "symbol": symbol,
        "agent_key": agent_key,
        "last_data_at": last_data_at,
        "age_minutes": age_min,
        "is_fresh": fresh,
        "checked_at": checked_at,
    }
    bind = session.get_bind()
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    insert_stmt = insert(AgentFreshnessStatus).values(**values)
    await session.execute(
        insert_stmt.on_conflict_do_update(
            index_elements=["profile_id", "agent_key"],
            set_={col: insert_stmt.excluded[col] for col in _UPSERT_SET_COLUMNS},
        )
    )


async def sweep_agent_freshness(
    *,
    session: AsyncSession,
    threshold_minutes: int = DEFAULT_FRESHNESS_THRESHOLD_MINUTES,
    now: datetime | None = None,
) -> dict[str, int]:
    """Recompute freshness for every active profile + enabled agent.

    Returns counters: ``profiles``, ``agents`` (rows written), ``fresh``,
    ``stale``, ``no_data`` (profiles with no history), ``events`` (data_stale
    emitted).
    """
    now = now or datetime.now(UTC)
    stats = {"profiles": 0, "agents": 0, "fresh": 0, "stale": 0, "no_data": 0, "events": 0}

    profiles = list(
        (
            await session.scalars(
                select(PersonalAnalysisProfile).where(PersonalAnalysisProfile.is_active.is_(True))
            )
        ).all()
    )

    for profile in profiles:
        stats["profiles"] += 1
        latest = await session.scalar(
            select(PersonalAnalysisHistory)
            .where(PersonalAnalysisHistory.profile_id == profile.id)
            .order_by(PersonalAnalysisHistory.created_at.desc())
            .limit(1)
        )
        reference = (
            normalize_to_utc(latest.core_completed_at or latest.created_at)
            if latest is not None
            else None
        )
        age = age_minutes(reference, now=now)
        age_int = int(round(age)) if age is not None else None
        fresh = is_fresh(reference, max_age_minutes=threshold_minutes, now=now)
        if reference is None:
            stats["no_data"] += 1

        enabled_agents = [key for key, on in (profile.agents or {}).items() if on]
        agent_keys = [PROFILE_AGENT_KEY, *enabled_agents]
        for agent_key in agent_keys:
            await _upsert_status(
                session,
                profile_id=profile.id,
                symbol=profile.symbol,
                agent_key=agent_key,
                last_data_at=reference,
                age_min=age_int,
                fresh=fresh,
                checked_at=now,
            )
            stats["agents"] += 1
            stats["fresh" if fresh else "stale"] += 1

        if not fresh:
            # One event per stale profile (not per agent) to keep the channel quiet.
            session.add(
                AutoTradeEvent(
                    user_id=profile.user_id,
                    config_id=None,
                    profile_id=profile.id,
                    history_id=latest.id if latest is not None else None,
                    position_id=None,
                    event_type=_EVENT_TYPE_DATA_STALE,
                    level=_EVENT_LEVEL_WARNING,
                    message=(
                        f"AI data for {profile.symbol} is stale "
                        f"(age {age_int if age_int is not None else 'n/a'} min "
                        f"> {threshold_minutes} min threshold)."
                    ),
                    payload={
                        "profile_id": profile.id,
                        "symbol": profile.symbol,
                        "age_minutes": age_int,
                        "threshold_minutes": threshold_minutes,
                        "stale_agents": agent_keys,
                    },
                )
            )
            stats["events"] += 1

    await session.commit()
    return stats
