from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.live_paper_profile import LivePaperProfile
from app.models.strategy import Strategy
from app.models.user import User
from app.schemas.admin import (
    AdminAutoTradeConfigRead,
    AdminAutoTradePositionRead,
    AdminLivePaperProfileRead,
    AdminRuntimePageRead,
    AdminRuntimeSnapshotResponse,
    AdminRuntimeSummaryRead,
    AdminStrategyRead,
    AdminUserRead,
    AdminUserRuntimeRead,
    AdminUserRuntimeStatsRead,
)


@dataclass(slots=True)
class _UserRuntimeCounters:
    total_strategies: int = 0
    active_strategies: int = 0
    auto_trade_configs: int = 0
    running_auto_trade_configs: int = 0
    auto_trade_positions: int = 0
    open_auto_trade_positions: int = 0
    live_paper_running: bool = False


class AdminRuntimeService:
    async def get_runtime_snapshot(
        self,
        *,
        session: AsyncSession,
        include_inactive_users: bool = True,
        positions_status: Literal["all", "open"] = "all",
        after_user_id: int | None = None,
        users_limit: int = 50,
        include_details: bool = True,
        strategies_limit_per_user: int = 50,
        configs_limit_per_user: int = 20,
        positions_limit_per_user: int = 100,
    ) -> AdminRuntimeSnapshotResponse:
        summary = await self._build_global_summary(
            session=session,
            include_inactive_users=include_inactive_users,
            positions_status=positions_status,
        )
        users, has_more, next_after_user_id = await self._load_user_page(
            session=session,
            include_inactive_users=include_inactive_users,
            after_user_id=after_user_id,
            users_limit=users_limit,
        )
        page = AdminRuntimePageRead(
            users_limit=users_limit,
            after_user_id=after_user_id,
            next_after_user_id=next_after_user_id,
            has_more=has_more,
        )
        if not users:
            return AdminRuntimeSnapshotResponse(
                generated_at=datetime.now(UTC),
                summary=summary,
                page=page,
                users=[],
            )

        user_ids = [user.id for user in users]
        stats_by_user = await self._load_user_stats(
            session=session,
            user_ids=user_ids,
            positions_status=positions_status,
        )

        strategies_by_user: dict[int, list[AdminStrategyRead]] = {}
        configs_by_user: dict[int, list[AdminAutoTradeConfigRead]] = {}
        positions_by_user: dict[int, list[AdminAutoTradePositionRead]] = {}
        live_profile_by_user: dict[int, AdminLivePaperProfileRead] = {}

        if include_details:
            strategy_rows = await self._load_limited_strategies(
                session=session,
                user_ids=user_ids,
                limit_per_user=strategies_limit_per_user,
            )
            for strategy in strategy_rows:
                strategies_by_user.setdefault(strategy.user_id, []).append(
                    AdminStrategyRead.model_validate(strategy)
                )

            config_rows = await self._load_limited_configs(
                session=session,
                user_ids=user_ids,
                limit_per_user=configs_limit_per_user,
            )
            for config in config_rows:
                configs_by_user.setdefault(config.user_id, []).append(
                    AdminAutoTradeConfigRead.model_validate(config)
                )

            position_rows = await self._load_limited_positions(
                session=session,
                user_ids=user_ids,
                positions_status=positions_status,
                limit_per_user=positions_limit_per_user,
            )
            for position in position_rows:
                positions_by_user.setdefault(position.user_id, []).append(
                    AdminAutoTradePositionRead.model_validate(position)
                )

            live_profile_rows = await self._load_live_paper_profiles(
                session=session,
                user_ids=user_ids,
            )
            live_profile_by_user = {
                profile.user_id: AdminLivePaperProfileRead.model_validate(profile)
                for profile in live_profile_rows
            }

        snapshots: list[AdminUserRuntimeRead] = []
        for user in users:
            counters = stats_by_user.get(user.id, _UserRuntimeCounters())
            user_strategies = strategies_by_user.get(user.id, [])
            user_configs = configs_by_user.get(user.id, [])
            user_positions = positions_by_user.get(user.id, [])

            snapshots.append(
                AdminUserRuntimeRead(
                    user=AdminUserRead.model_validate(user),
                    stats=AdminUserRuntimeStatsRead(
                        total_strategies=counters.total_strategies,
                        active_strategies=counters.active_strategies,
                        auto_trade_configs=counters.auto_trade_configs,
                        running_auto_trade_configs=counters.running_auto_trade_configs,
                        auto_trade_positions=counters.auto_trade_positions,
                        open_auto_trade_positions=counters.open_auto_trade_positions,
                        live_paper_running=counters.live_paper_running,
                    ),
                    strategies_truncated=(
                        include_details and len(user_strategies) < counters.total_strategies
                    ),
                    auto_trade_configs_truncated=(
                        include_details and len(user_configs) < counters.auto_trade_configs
                    ),
                    auto_trade_positions_truncated=(
                        include_details and len(user_positions) < counters.auto_trade_positions
                    ),
                    strategies=user_strategies,
                    auto_trade_configs=user_configs,
                    auto_trade_positions=user_positions,
                    live_paper_profile=live_profile_by_user.get(user.id),
                )
            )

        return AdminRuntimeSnapshotResponse(
            generated_at=datetime.now(UTC),
            summary=summary,
            page=page,
            users=snapshots,
        )

    async def _load_user_page(
        self,
        *,
        session: AsyncSession,
        include_inactive_users: bool,
        after_user_id: int | None,
        users_limit: int,
    ) -> tuple[list[User], bool, int | None]:
        stmt = select(User)
        if not include_inactive_users:
            stmt = stmt.where(User.is_active.is_(True))
        if after_user_id is not None:
            stmt = stmt.where(User.id > after_user_id)
        stmt = stmt.order_by(User.id.asc()).limit(users_limit + 1)

        rows = list((await session.scalars(stmt)).all())
        has_more = len(rows) > users_limit
        users = rows[:users_limit]
        next_after_user_id = users[-1].id if has_more and users else None
        return users, has_more, next_after_user_id

    async def _build_global_summary(
        self,
        *,
        session: AsyncSession,
        include_inactive_users: bool,
        positions_status: Literal["all", "open"],
    ) -> AdminRuntimeSummaryRead:
        users_stmt = select(
            func.count(User.id),
            func.sum(case((User.is_active.is_(True), 1), else_=0)),
            func.sum(case((User.is_admin.is_(True), 1), else_=0)),
        )
        if not include_inactive_users:
            users_stmt = users_stmt.where(User.is_active.is_(True))
        total_users, active_users, admin_users = (await session.execute(users_stmt)).one()

        strategies_stmt = (
            select(
                func.count(Strategy.id),
                func.sum(case((Strategy.is_active.is_(True), 1), else_=0)),
            )
            .join(User, User.id == Strategy.user_id)
        )
        if not include_inactive_users:
            strategies_stmt = strategies_stmt.where(User.is_active.is_(True))
        total_strategies, active_strategies = (await session.execute(strategies_stmt)).one()

        configs_stmt = (
            select(
                func.count(AutoTradeConfig.id),
                func.sum(case((AutoTradeConfig.is_running.is_(True), 1), else_=0)),
            )
            .join(User, User.id == AutoTradeConfig.user_id)
        )
        if not include_inactive_users:
            configs_stmt = configs_stmt.where(User.is_active.is_(True))
        total_configs, running_configs = (await session.execute(configs_stmt)).one()

        positions_stmt = (
            select(
                func.count(AutoTradePosition.id),
                func.sum(case((AutoTradePosition.status == "open", 1), else_=0)),
            )
            .join(User, User.id == AutoTradePosition.user_id)
        )
        if not include_inactive_users:
            positions_stmt = positions_stmt.where(User.is_active.is_(True))
        if positions_status == "open":
            positions_stmt = positions_stmt.where(AutoTradePosition.status == "open")
        total_positions, open_positions = (await session.execute(positions_stmt)).one()

        live_stmt = (
            select(func.count(LivePaperProfile.id))
            .join(User, User.id == LivePaperProfile.user_id)
            .where(LivePaperProfile.is_running.is_(True))
        )
        if not include_inactive_users:
            live_stmt = live_stmt.where(User.is_active.is_(True))
        (running_live_paper_profiles,) = (await session.execute(live_stmt)).one()

        return AdminRuntimeSummaryRead(
            total_users=self._to_int(total_users),
            active_users=self._to_int(active_users),
            admin_users=self._to_int(admin_users),
            total_strategies=self._to_int(total_strategies),
            active_strategies=self._to_int(active_strategies),
            total_auto_trade_configs=self._to_int(total_configs),
            running_auto_trade_configs=self._to_int(running_configs),
            total_auto_trade_positions=self._to_int(total_positions),
            open_auto_trade_positions=self._to_int(open_positions),
            running_live_paper_profiles=self._to_int(running_live_paper_profiles),
        )

    async def _load_user_stats(
        self,
        *,
        session: AsyncSession,
        user_ids: list[int],
        positions_status: Literal["all", "open"],
    ) -> dict[int, _UserRuntimeCounters]:
        stats = {user_id: _UserRuntimeCounters() for user_id in user_ids}

        strategies_stmt = (
            select(
                Strategy.user_id,
                func.count(Strategy.id),
                func.sum(case((Strategy.is_active.is_(True), 1), else_=0)),
            )
            .where(Strategy.user_id.in_(user_ids))
            .group_by(Strategy.user_id)
        )
        for user_id, total, active in (await session.execute(strategies_stmt)).all():
            counters = stats[user_id]
            counters.total_strategies = self._to_int(total)
            counters.active_strategies = self._to_int(active)

        configs_stmt = (
            select(
                AutoTradeConfig.user_id,
                func.count(AutoTradeConfig.id),
                func.sum(case((AutoTradeConfig.is_running.is_(True), 1), else_=0)),
            )
            .where(AutoTradeConfig.user_id.in_(user_ids))
            .group_by(AutoTradeConfig.user_id)
        )
        for user_id, total, running in (await session.execute(configs_stmt)).all():
            counters = stats[user_id]
            counters.auto_trade_configs = self._to_int(total)
            counters.running_auto_trade_configs = self._to_int(running)

        positions_stmt = (
            select(
                AutoTradePosition.user_id,
                func.count(AutoTradePosition.id),
                func.sum(case((AutoTradePosition.status == "open", 1), else_=0)),
            )
            .where(AutoTradePosition.user_id.in_(user_ids))
        )
        if positions_status == "open":
            positions_stmt = positions_stmt.where(AutoTradePosition.status == "open")
        positions_stmt = positions_stmt.group_by(AutoTradePosition.user_id)
        for user_id, total, open_count in (await session.execute(positions_stmt)).all():
            counters = stats[user_id]
            counters.auto_trade_positions = self._to_int(total)
            counters.open_auto_trade_positions = self._to_int(open_count)

        live_stmt = select(
            LivePaperProfile.user_id,
            LivePaperProfile.is_running,
        ).where(LivePaperProfile.user_id.in_(user_ids))
        for user_id, is_running in (await session.execute(live_stmt)).all():
            stats[user_id].live_paper_running = bool(is_running)

        return stats

    async def _load_limited_strategies(
        self,
        *,
        session: AsyncSession,
        user_ids: list[int],
        limit_per_user: int,
    ) -> list[Strategy]:
        ranked = (
            select(
                Strategy.id.label("row_id"),
                func.row_number()
                .over(
                    partition_by=Strategy.user_id,
                    order_by=(Strategy.created_at.desc(), Strategy.id.desc()),
                )
                .label("rn"),
            )
            .where(Strategy.user_id.in_(user_ids))
            .subquery()
        )
        stmt = (
            select(Strategy)
            .join(ranked, Strategy.id == ranked.c.row_id)
            .where(ranked.c.rn <= limit_per_user)
            .order_by(Strategy.user_id.asc(), Strategy.created_at.desc(), Strategy.id.desc())
        )
        return list((await session.scalars(stmt)).all())

    async def _load_limited_configs(
        self,
        *,
        session: AsyncSession,
        user_ids: list[int],
        limit_per_user: int,
    ) -> list[AutoTradeConfig]:
        ranked = (
            select(
                AutoTradeConfig.id.label("row_id"),
                func.row_number()
                .over(
                    partition_by=AutoTradeConfig.user_id,
                    order_by=(AutoTradeConfig.created_at.desc(), AutoTradeConfig.id.desc()),
                )
                .label("rn"),
            )
            .where(AutoTradeConfig.user_id.in_(user_ids))
            .subquery()
        )
        stmt = (
            select(AutoTradeConfig)
            .join(ranked, AutoTradeConfig.id == ranked.c.row_id)
            .where(ranked.c.rn <= limit_per_user)
            .order_by(
                AutoTradeConfig.user_id.asc(),
                AutoTradeConfig.created_at.desc(),
                AutoTradeConfig.id.desc(),
            )
        )
        return list((await session.scalars(stmt)).all())

    async def _load_limited_positions(
        self,
        *,
        session: AsyncSession,
        user_ids: list[int],
        positions_status: Literal["all", "open"],
        limit_per_user: int,
    ) -> list[AutoTradePosition]:
        ranked_stmt = select(
            AutoTradePosition.id.label("row_id"),
            func.row_number()
            .over(
                partition_by=AutoTradePosition.user_id,
                order_by=(AutoTradePosition.opened_at.desc(), AutoTradePosition.id.desc()),
            )
            .label("rn"),
        ).where(AutoTradePosition.user_id.in_(user_ids))
        if positions_status == "open":
            ranked_stmt = ranked_stmt.where(AutoTradePosition.status == "open")
        ranked = ranked_stmt.subquery()

        stmt = (
            select(AutoTradePosition)
            .join(ranked, AutoTradePosition.id == ranked.c.row_id)
            .where(ranked.c.rn <= limit_per_user)
            .order_by(
                AutoTradePosition.user_id.asc(),
                AutoTradePosition.opened_at.desc(),
                AutoTradePosition.id.desc(),
            )
        )
        return list((await session.scalars(stmt)).all())

    async def _load_live_paper_profiles(
        self,
        *,
        session: AsyncSession,
        user_ids: list[int],
    ) -> list[LivePaperProfile]:
        stmt = select(LivePaperProfile).where(LivePaperProfile.user_id.in_(user_ids))
        stmt = stmt.order_by(LivePaperProfile.user_id.asc(), LivePaperProfile.id.desc())
        return list((await session.scalars(stmt)).all())

    @staticmethod
    def _to_int(value: object | None) -> int:
        if value is None:
            return 0
        return int(value)
