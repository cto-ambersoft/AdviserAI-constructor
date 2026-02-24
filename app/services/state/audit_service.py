from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.schemas.audit import AuditLogCreate, AuditLogRead


class AuditService:
    async def list_events(
        self,
        session: AsyncSession,
        actor: str,
        limit: int = 200,
    ) -> list[AuditLogRead]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.actor == actor)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        rows = await session.scalars(stmt)
        return [AuditLogRead.model_validate(row) for row in rows.all()]

    async def create_event(
        self,
        session: AsyncSession,
        payload: AuditLogCreate,
        actor: str,
    ) -> AuditLogRead:
        row = AuditLog(
            actor=actor,
            event=payload.event,
            reason=payload.reason,
            target_type=payload.target_type,
            target_id=payload.target_id,
            payload=payload.payload,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return AuditLogRead.model_validate(row)
