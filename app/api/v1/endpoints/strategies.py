from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.strategy import (
    STRATEGY_DEFAULT_TYPE,
    STRATEGY_DEFAULT_VERSION,
    STRATEGY_TYPES,
    StrategyCreate,
    StrategyMetaResponse,
    StrategyRead,
)
from app.services.strategy_manager.service import StrategyManagerService

router = APIRouter()
strategy_service = StrategyManagerService()


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
    return await strategy_service.create_strategy(session, payload, user_id=current_user.id)


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete strategy")
async def delete_strategy(strategy_id: int, session: DbSession, current_user: CurrentUser) -> None:
    deleted = await strategy_service.delete_strategy(session, strategy_id, user_id=current_user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found.")
