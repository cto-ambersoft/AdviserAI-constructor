from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession
from app.schemas.audit import AuditLogCreate
from app.schemas.strategy import (
    STRATEGY_DEFAULT_TYPE,
    STRATEGY_DEFAULT_VERSION,
    STRATEGY_TYPES,
    StrategyCreate,
    StrategyMetaResponse,
    StrategyRead,
    StrategyUpdate,
)
from app.services.state.audit_service import AuditService
from app.services.strategy_manager.service import StrategyManagerService

router = APIRouter()
strategy_service = StrategyManagerService()
audit_service = AuditService()


def _extract_indicators(config: dict[str, object] | None) -> list[str]:
    if not config:
        return []
    raw = config.get("enabled")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


@router.get("/meta", response_model=StrategyMetaResponse, summary="Get strategy metadata")
async def get_strategy_meta() -> StrategyMetaResponse:
    return StrategyMetaResponse(
        supported_strategy_types=list(STRATEGY_TYPES),
        default_strategy_type=STRATEGY_DEFAULT_TYPE,
        default_version=STRATEGY_DEFAULT_VERSION,
        name_min_length=1,
        name_max_length=120,
        strategy_type_min_length=1,
        strategy_type_max_length=64,
        version_min_length=1,
        version_max_length=32,
    )


@router.get("/", response_model=list[StrategyRead], summary="List strategies")
async def list_strategies(session: DbSession, current_user: CurrentUser) -> list[StrategyRead]:
    return await strategy_service.list_strategies(session, user_id=current_user.id)


@router.post("/", response_model=StrategyRead, summary="Create strategy")
async def create_strategy(
    payload: StrategyCreate,
    session: DbSession,
    current_user: CurrentUser,
) -> StrategyRead:
    try:
        created = await strategy_service.create_strategy(session, payload, user_id=current_user.id)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Strategy with this name already exists.",
        ) from exc
    indicators = _extract_indicators(created.config)
    await audit_service.create_event(
        session=session,
        actor=current_user.email,
        payload=AuditLogCreate(
            event="SAVE_STRATEGY",
            reason="User created strategy.",
            target_type="strategy",
            target_id=str(created.id),
            payload={
                "strategy_id": created.id,
                "name": created.name,
                "strategy_type": created.strategy_type,
                "indicators": indicators,
            },
        ),
    )
    return created


@router.put("/{strategy_id}", response_model=StrategyRead, summary="Update strategy")
async def update_strategy(
    strategy_id: int,
    payload: StrategyUpdate,
    session: DbSession,
    current_user: CurrentUser,
) -> StrategyRead:
    before = await strategy_service.get_strategy(session, strategy_id=strategy_id, user_id=current_user.id)
    if before is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found.")
    try:
        updated = await strategy_service.update_strategy(
            session,
            strategy_id=strategy_id,
            payload=payload,
            user_id=current_user.id,
        )
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Strategy with this name already exists.",
        ) from exc
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found.")
    prev_indicators = set(_extract_indicators(before.config))
    next_indicators = set(_extract_indicators(updated.config))
    await audit_service.create_event(
        session=session,
        actor=current_user.email,
        payload=AuditLogCreate(
            event="UPDATE_STRATEGY",
            reason="User updated strategy.",
            target_type="strategy",
            target_id=str(updated.id),
            payload={
                "strategy_id": updated.id,
                "name": updated.name,
                "updated_fields": sorted(payload.model_dump(exclude_none=True).keys()),
            },
        ),
    )
    if prev_indicators != next_indicators:
        await audit_service.create_event(
            session=session,
            actor=current_user.email,
            payload=AuditLogCreate(
                event="INDICATORS_CHANGE",
                reason="User changed strategy indicators.",
                target_type="strategy",
                target_id=str(updated.id),
                payload={
                    "strategy_id": updated.id,
                    "name": updated.name,
                    "before": sorted(prev_indicators),
                    "after": sorted(next_indicators),
                    "added": sorted(next_indicators - prev_indicators),
                    "removed": sorted(prev_indicators - next_indicators),
                },
            ),
        )
    return updated


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete strategy")
async def delete_strategy(strategy_id: int, session: DbSession, current_user: CurrentUser) -> None:
    deleted = await strategy_service.delete_strategy(session, strategy_id, user_id=current_user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found.")
