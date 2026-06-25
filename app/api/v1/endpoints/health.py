from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.agent_freshness_status import AgentFreshnessStatus
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.schemas.personal_analysis import AgentFreshnessRead, AgentFreshnessResponse

router = APIRouter()


@router.get("/health", summary="Readiness probe")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/health/agents",
    response_model=AgentFreshnessResponse,
    summary="AI agent data-freshness snapshot for the current user",
)
async def get_agent_freshness(
    session: DbSession,
    current_user: CurrentUser,
    is_fresh: bool | None = Query(default=None),
) -> AgentFreshnessResponse:
    # Scoped to the caller's own profiles; optional is_fresh filter.
    stmt = (
        select(AgentFreshnessStatus)
        .join(
            PersonalAnalysisProfile,
            AgentFreshnessStatus.profile_id == PersonalAnalysisProfile.id,
        )
        .where(PersonalAnalysisProfile.user_id == current_user.id)
    )
    if is_fresh is not None:
        stmt = stmt.where(AgentFreshnessStatus.is_fresh.is_(is_fresh))
    stmt = stmt.order_by(
        AgentFreshnessStatus.profile_id.asc(),
        AgentFreshnessStatus.agent_key.asc(),
    )
    rows = (await session.scalars(stmt)).all()
    return AgentFreshnessResponse(statuses=[AgentFreshnessRead.model_validate(row) for row in rows])
