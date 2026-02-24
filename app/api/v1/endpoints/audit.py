from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.schemas.audit import (
    AUDIT_DEFAULT_TARGET_ID,
    AUDIT_DEFAULT_TARGET_TYPE,
    AUDIT_EVENTS,
    AUDIT_TARGET_TYPES,
    AuditLogCreate,
    AuditLogRead,
    AuditMetaResponse,
)
from app.services.state.audit_service import AuditService

router = APIRouter()
audit_service = AuditService()
AUDIT_LIST_LIMIT_DEFAULT = 200
AUDIT_LIST_LIMIT_MIN = 1
AUDIT_LIST_LIMIT_MAX = 1000


@router.get("/meta", response_model=AuditMetaResponse, summary="Get audit metadata")
async def get_audit_meta() -> AuditMetaResponse:
    return AuditMetaResponse(
        suggested_events=list(AUDIT_EVENTS),
        suggested_target_types=list(AUDIT_TARGET_TYPES),
        default_target_type=AUDIT_DEFAULT_TARGET_TYPE,
        default_target_id=AUDIT_DEFAULT_TARGET_ID,
        list_limit_default=AUDIT_LIST_LIMIT_DEFAULT,
        list_limit_min=AUDIT_LIST_LIMIT_MIN,
        list_limit_max=AUDIT_LIST_LIMIT_MAX,
    )


@router.get("/", response_model=list[AuditLogRead], summary="List audit events")
async def list_audit_events(
    session: DbSession,
    current_user: CurrentUser,
    limit: int = Query(
        default=AUDIT_LIST_LIMIT_DEFAULT,
        ge=AUDIT_LIST_LIMIT_MIN,
        le=AUDIT_LIST_LIMIT_MAX,
    ),
) -> list[AuditLogRead]:
    return await audit_service.list_events(session, actor=current_user.email, limit=limit)


@router.post("/events", response_model=AuditLogRead, summary="Create audit event")
async def create_audit_event(
    payload: AuditLogCreate,
    session: DbSession,
    current_user: CurrentUser,
) -> AuditLogRead:
    return await audit_service.create_event(session, payload, actor=current_user.email)
