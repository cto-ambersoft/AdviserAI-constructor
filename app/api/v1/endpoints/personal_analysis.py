from datetime import datetime

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.personal_analysis import (
    PERSONAL_ANALYSIS_AGENT_NAMES,
    PersonalAnalysisDefaultsRead,
    PersonalAnalysisHistoryRead,
    PersonalAnalysisJobRead,
    PersonalAnalysisManualTriggerRequest,
    PersonalAnalysisManualTriggerResponse,
    PersonalAnalysisProfileCreate,
    PersonalAnalysisProfileRead,
    PersonalAnalysisProfileUpdate,
    get_personal_analysis_defaults,
)
from app.services.personal_analysis.provider import AnalysisProviderError
from app.services.personal_analysis.service import PersonalAnalysisService

router = APIRouter()
personal_analysis_service = PersonalAnalysisService()


@router.get(
    "/defaults",
    response_model=PersonalAnalysisDefaultsRead,
    summary="Get personal analysis default agents and weights",
)
async def get_personal_analysis_defaults_endpoint() -> PersonalAnalysisDefaultsRead:
    agents, weights = get_personal_analysis_defaults()
    return PersonalAnalysisDefaultsRead(
        available_agents=list(PERSONAL_ANALYSIS_AGENT_NAMES),
        agents=agents,
        agent_weights=weights,
    )


@router.get(
    "/profiles",
    response_model=list[PersonalAnalysisProfileRead],
    summary="List personal analysis profiles",
)
async def list_personal_analysis_profiles(
    session: DbSession,
    current_user: CurrentUser,
) -> list[PersonalAnalysisProfileRead]:
    rows = await personal_analysis_service.list_profiles(session=session, user_id=current_user.id)
    return [PersonalAnalysisProfileRead.model_validate(row) for row in rows]


@router.post(
    "/profiles",
    response_model=PersonalAnalysisProfileRead,
    summary="Create personal analysis profile",
)
async def create_personal_analysis_profile(
    payload: PersonalAnalysisProfileCreate,
    session: DbSession,
    current_user: CurrentUser,
) -> PersonalAnalysisProfileRead:
    try:
        created = await personal_analysis_service.create_profile(
            session=session,
            user_id=current_user.id,
            payload=payload,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return PersonalAnalysisProfileRead.model_validate(created)


@router.put(
    "/profiles/{profile_id}",
    response_model=PersonalAnalysisProfileRead,
    summary="Update personal analysis profile",
)
async def update_personal_analysis_profile(
    profile_id: int,
    payload: PersonalAnalysisProfileUpdate,
    session: DbSession,
    current_user: CurrentUser,
) -> PersonalAnalysisProfileRead:
    try:
        updated = await personal_analysis_service.update_profile(
            session=session,
            user_id=current_user.id,
            profile_id=profile_id,
            payload=payload,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return PersonalAnalysisProfileRead.model_validate(updated)


@router.delete(
    "/profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate personal analysis profile",
)
async def deactivate_personal_analysis_profile(
    profile_id: int,
    session: DbSession,
    current_user: CurrentUser,
) -> None:
    deleted = await personal_analysis_service.deactivate_profile(
        session=session,
        user_id=current_user.id,
        profile_id=profile_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal analysis profile not found.",
        )


@router.post(
    "/profiles/{profile_id}/trigger",
    response_model=PersonalAnalysisManualTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger personal analysis job",
)
async def trigger_personal_analysis_profile(
    profile_id: int,
    payload: PersonalAnalysisManualTriggerRequest,
    session: DbSession,
    current_user: CurrentUser,
) -> PersonalAnalysisManualTriggerResponse:
    try:
        job = await personal_analysis_service.trigger_profile(
            session=session,
            user_id=current_user.id,
            profile_id=profile_id,
            overrides=payload,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except AnalysisProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return PersonalAnalysisManualTriggerResponse(
        trade_job_id=job.id,
        core_job_id=job.core_job_id,
        status=job.status,
        created_at=job.created_at,
    )


@router.get(
    "/jobs/{trade_job_id}",
    response_model=PersonalAnalysisJobRead,
    summary="Get personal analysis job status",
)
async def get_personal_analysis_job(
    trade_job_id: str,
    session: DbSession,
    current_user: CurrentUser,
) -> PersonalAnalysisJobRead:
    row = await personal_analysis_service.get_job(
        session=session,
        user_id=current_user.id,
        trade_job_id=trade_job_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal analysis job not found.",
        )
    return PersonalAnalysisJobRead.model_validate(row)


@router.get(
    "/history",
    response_model=list[PersonalAnalysisHistoryRead],
    summary="Get personal analysis history",
)
async def list_personal_analysis_history(
    session: DbSession,
    current_user: CurrentUser,
    profile_id: int | None = None,
    limit: int = 50,
    before: datetime | None = None,
) -> list[PersonalAnalysisHistoryRead]:
    limit = max(1, min(limit, 200))
    rows = await personal_analysis_service.get_history(
        session=session,
        user_id=current_user.id,
        profile_id=profile_id,
        limit=limit,
        before=before,
    )
    return [PersonalAnalysisHistoryRead.model_validate(row) for row in rows]


@router.get(
    "/latest",
    response_model=PersonalAnalysisHistoryRead,
    summary="Get latest personal analysis result",
)
async def get_latest_personal_analysis_history(
    session: DbSession,
    current_user: CurrentUser,
    profile_id: int | None = None,
    symbol: str | None = None,
) -> PersonalAnalysisHistoryRead:
    row = await personal_analysis_service.get_latest(
        session=session,
        user_id=current_user.id,
        profile_id=profile_id,
        symbol=symbol,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal analysis history not found.",
        )
    return PersonalAnalysisHistoryRead.model_validate(row)
