from typing import Literal

from fastapi import APIRouter, Query

from app.api.deps import CurrentAdminUser, DbSession
from app.schemas.admin import AdminRuntimeSnapshotResponse
from app.services.admin.service import AdminRuntimeService

router = APIRouter()
admin_runtime_service = AdminRuntimeService()


@router.get(
    "/runtime",
    response_model=AdminRuntimeSnapshotResponse,
    summary="Get runtime data for all users (admin)",
)
async def get_admin_runtime_snapshot(
    session: DbSession,
    _current_admin: CurrentAdminUser,
    include_inactive_users: bool = Query(default=True),
    positions_status: Literal["all", "open"] = Query(default="all"),
    after_user_id: int | None = Query(default=None, ge=1),
    users_limit: int = Query(default=50, ge=1, le=200),
    include_details: bool = Query(default=True),
    strategies_limit_per_user: int = Query(default=50, ge=1, le=200),
    configs_limit_per_user: int = Query(default=20, ge=1, le=100),
    positions_limit_per_user: int = Query(default=100, ge=1, le=500),
) -> AdminRuntimeSnapshotResponse:
    return await admin_runtime_service.get_runtime_snapshot(
        session=session,
        include_inactive_users=include_inactive_users,
        positions_status=positions_status,
        after_user_id=after_user_id,
        users_limit=users_limit,
        include_details=include_details,
        strategies_limit_per_user=strategies_limit_per_user,
        configs_limit_per_user=configs_limit_per_user,
        positions_limit_per_user=positions_limit_per_user,
    )
