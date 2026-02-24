from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategy import Strategy
from app.schemas.strategy import StrategyCreate, StrategyRead


class StrategyManagerService:
    async def list_strategies(self, session: AsyncSession, user_id: int) -> list[StrategyRead]:
        rows = await session.scalars(
            select(Strategy).where(Strategy.user_id == user_id).order_by(Strategy.created_at.desc())
        )
        return [StrategyRead.model_validate(row) for row in rows.all()]

    async def create_strategy(
        self,
        session: AsyncSession,
        payload: StrategyCreate,
        user_id: int,
    ) -> StrategyRead:
        row = Strategy(
            user_id=user_id,
            name=payload.name,
            strategy_type=payload.strategy_type,
            version=payload.version,
            description=payload.description,
            is_active=payload.is_active,
            config=payload.config,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return StrategyRead.model_validate(row)

    async def delete_strategy(self, session: AsyncSession, strategy_id: int, user_id: int) -> bool:
        row = await session.scalar(
            select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id)
        )
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True
