from datetime import UTC, datetime
import math
from typing import Any

from sqlalchemy import Select, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.live_paper_event import LivePaperEvent
from app.models.live_paper_profile import LivePaperProfile
from app.models.live_paper_trade import LivePaperTrade
from app.models.strategy import Strategy
from app.schemas.backtest import (
    AtrOrderBlockRequest,
    GridBotRequest,
    IntradayMomentumRequest,
    KnifeCatcherRequest,
    VwapBacktestRequest,
)
from app.schemas.live import LivePaperProfileUpsertRequest
from app.services.backtesting.service import BacktestingService
from app.services.execution.errors import ExchangeServiceError

_STRATEGY_MODEL_MAP: dict[str, type] = {
    "builder_vwap": VwapBacktestRequest,
    "atr_order_block": AtrOrderBlockRequest,
    "knife_catcher": KnifeCatcherRequest,
    "grid_bot": GridBotRequest,
    "intraday_momentum": IntradayMomentumRequest,
}


class LivePaperService:
    def __init__(self, backtesting_service: BacktestingService | None = None) -> None:
        self._backtesting = backtesting_service or BacktestingService()

    async def get_profile(self, session: AsyncSession, user_id: int) -> LivePaperProfile | None:
        return await self._get_profile(session=session, user_id=user_id, for_update=False)

    async def upsert_profile(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        payload: LivePaperProfileUpsertRequest,
    ) -> LivePaperProfile:
        strategy = await self._get_user_strategy(session, user_id=user_id, strategy_id=payload.strategy_id)
        profile = await self._get_profile(session=session, user_id=user_id, for_update=True)
        now = datetime.now(UTC)

        if profile is None:
            profile = LivePaperProfile(
                user_id=user_id,
                strategy_id=strategy.id,
                strategy_revision=1,
                is_running=False,
                total_balance_usdt=float(payload.total_balance_usdt),
                per_trade_usdt=float(payload.per_trade_usdt),
                last_processed_at=None,
                last_poll_at=None,
            )
            session.add(profile)
            await session.flush()
            await session.commit()
            await session.refresh(profile)
            return profile

        if profile.strategy_id != strategy.id:
            previous_strategy_id = profile.strategy_id
            previous_revision = profile.strategy_revision
            previous_total_balance = float(profile.total_balance_usdt)
            previous_per_trade = float(profile.per_trade_usdt)
            previous_strategy = await self._get_user_strategy(
                session,
                user_id=user_id,
                strategy_id=previous_strategy_id,
            )
            await self._catch_up_for_strategy(
                session=session,
                profile=profile,
                strategy=previous_strategy,
                until=now,
            )
            previous_snapshot = await self._build_revision_snapshot(
                session=session,
                profile_id=profile.id,
                strategy_revision=previous_revision,
                initial_balance=previous_total_balance,
            )
            profile.strategy_id = strategy.id
            profile.strategy_revision += 1
            # If user switches strategy while stopped, we should replay full
            # backlog of the new strategy on next play/poll.
            profile.last_processed_at = now if profile.is_running else None
            session.add(
                LivePaperEvent(
                    profile_id=profile.id,
                    strategy_revision=profile.strategy_revision,
                    event_type="strategy_switched",
                    event_time=now,
                    payload={
                        "from_strategy_id": previous_strategy_id,
                        "to_strategy_id": strategy.id,
                        "from_revision": previous_revision,
                        "to_revision": profile.strategy_revision,
                        "from_total_balance_usdt": previous_total_balance,
                        "from_per_trade_usdt": previous_per_trade,
                        "to_total_balance_usdt": float(payload.total_balance_usdt),
                        "to_per_trade_usdt": float(payload.per_trade_usdt),
                        "snapshot": previous_snapshot,
                    },
                )
            )

        profile.total_balance_usdt = float(payload.total_balance_usdt)
        profile.per_trade_usdt = float(payload.per_trade_usdt)
        await session.commit()
        await session.refresh(profile)
        return profile

    async def set_running(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        is_running: bool,
    ) -> LivePaperProfile:
        profile = await self._get_profile(session=session, user_id=user_id, for_update=True)
        if profile is None:
            raise LookupError("Live paper profile not found.")
        now = datetime.now(UTC)
        turning_on = is_running and not profile.is_running
        profile.is_running = is_running
        if turning_on:
            # Live paper metrics should start from explicit user play.
            profile.last_processed_at = now
            session.add(
                LivePaperEvent(
                    profile_id=profile.id,
                    strategy_revision=profile.strategy_revision,
                    event_type="paper_started",
                    event_time=now,
                    payload={
                        "strategy_id": profile.strategy_id,
                        "strategy_revision": profile.strategy_revision,
                        "total_balance_usdt": float(profile.total_balance_usdt),
                        "per_trade_usdt": float(profile.per_trade_usdt),
                    },
                )
            )
        await session.commit()
        await session.refresh(profile)
        return profile

    async def poll_profile(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        last_trade_id: int | None = None,
        last_event_id: int | None = None,
        limit: int = 500,
    ) -> tuple[
        LivePaperProfile,
        list[LivePaperTrade],
        list[LivePaperEvent],
        dict[str, Any],
    ]:
        profile = await self._get_profile(session=session, user_id=user_id, for_update=True)
        if profile is None:
            raise LookupError("Live paper profile not found.")

        now = datetime.now(UTC)
        if profile.is_running:
            strategy = await self._get_user_strategy(
                session,
                user_id=user_id,
                strategy_id=profile.strategy_id,
            )
            try:
                await self._catch_up_for_strategy(
                    session=session,
                    profile=profile,
                    strategy=strategy,
                    until=now,
                )
            except ExchangeServiceError as exc:
                session.add(
                    LivePaperEvent(
                        profile_id=profile.id,
                        strategy_revision=profile.strategy_revision,
                        event_type="paper_poll_error",
                        event_time=now,
                        payload={
                            "code": exc.code,
                            "message": exc.message,
                            "retryable": bool(exc.retryable),
                            "strategy_id": strategy.id,
                            "strategy_revision": profile.strategy_revision,
                        },
                    )
                )
            except Exception as exc:
                session.add(
                    LivePaperEvent(
                        profile_id=profile.id,
                        strategy_revision=profile.strategy_revision,
                        event_type="paper_poll_error",
                        event_time=now,
                        payload={
                            "code": "internal_error",
                            "message": str(exc),
                            "retryable": True,
                            "strategy_id": strategy.id,
                            "strategy_revision": profile.strategy_revision,
                        },
                    )
                )
            profile.last_poll_at = now
            await session.commit()
            await session.refresh(profile)

        stats_start_time = await self._resolve_stats_start_time(session=session, profile=profile)
        live_stmt: Select[tuple[LivePaperTrade]] = select(LivePaperTrade).where(
            LivePaperTrade.profile_id == profile.id
        )
        if last_trade_id is not None:
            live_stmt = live_stmt.where(LivePaperTrade.id > last_trade_id)
        if stats_start_time is None:
            live_stmt = live_stmt.where(False)  # no explicit live start yet
        else:
            live_stmt = live_stmt.where(LivePaperTrade.exit_time > stats_start_time)
        live_stmt = live_stmt.order_by(LivePaperTrade.id.asc()).limit(limit)
        live_trades_since_start = list((await session.scalars(live_stmt)).all())

        events_stmt: Select[tuple[LivePaperEvent]] = select(LivePaperEvent).where(
            LivePaperEvent.profile_id == profile.id
        )
        if last_event_id is not None:
            events_stmt = events_stmt.where(LivePaperEvent.id > last_event_id)
        events_stmt = events_stmt.order_by(LivePaperEvent.id.asc()).limit(limit)
        events = list((await session.scalars(events_stmt)).all())

        metrics = await self._build_metrics(session=session, profile=profile)
        return profile, live_trades_since_start, events, metrics

    async def _catch_up_for_strategy(
        self,
        *,
        session: AsyncSession,
        profile: LivePaperProfile,
        strategy: Strategy,
        until: datetime,
    ) -> None:
        backtest_result = await self._run_backtest_for_strategy(strategy=strategy, profile=profile)
        raw_trades = backtest_result.get("trades", [])
        if not isinstance(raw_trades, list):
            return

        last_processed_at = _normalize_time(profile.last_processed_at)
        max_seen_exit_time = last_processed_at
        for trade in raw_trades:
            if not isinstance(trade, dict):
                continue
            if str(trade.get("exit_reason") or "").upper() == "OPEN":
                continue
            entry_time = _parse_time(trade.get("entry_time"))
            exit_time = _parse_time(trade.get("exit_time"))
            if entry_time is None or exit_time is None:
                continue
            if last_processed_at is not None and exit_time <= last_processed_at:
                continue
            if exit_time > until:
                continue
            if max_seen_exit_time is None or exit_time > max_seen_exit_time:
                max_seen_exit_time = exit_time

            row = LivePaperTrade(
                profile_id=profile.id,
                strategy_id=strategy.id,
                strategy_revision=profile.strategy_revision,
                side=str(trade.get("side") or "UNKNOWN"),
                entry_time=entry_time,
                exit_time=exit_time,
                entry_price=float(trade.get("entry") or 0.0),
                exit_price=float(trade.get("exit") or 0.0),
                pnl_usdt=float(trade.get("pnl_usdt") or 0.0),
                status="closed",
                raw_payload=_json_sanitize(trade),
            )
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                continue

        profile.last_processed_at = max_seen_exit_time

    async def _run_backtest_for_strategy(
        self,
        *,
        strategy: Strategy,
        profile: LivePaperProfile,
    ) -> dict[str, Any]:
        payload = self._build_payload(
            strategy_type=strategy.strategy_type,
            config=dict(strategy.config or {}),
            profile=profile,
        )
        strategy_type = strategy.strategy_type
        if strategy_type == "builder_vwap":
            return await self._backtesting.run_vwap(payload)
        if strategy_type == "atr_order_block":
            return await self._backtesting.run_atr_order_block(payload)
        if strategy_type == "knife_catcher":
            return await self._backtesting.run_knife(payload)
        if strategy_type == "grid_bot":
            return await self._backtesting.run_grid(payload)
        if strategy_type == "intraday_momentum":
            return await self._backtesting.run_intraday(payload)
        raise ValueError(f"Unsupported strategy_type '{strategy_type}'.")

    def _build_payload(
        self,
        *,
        strategy_type: str,
        config: dict[str, Any],
        profile: LivePaperProfile,
    ) -> dict[str, Any]:
        model_cls = _STRATEGY_MODEL_MAP.get(strategy_type)
        if model_cls is None:
            raise ValueError(f"Unsupported strategy_type '{strategy_type}'.")
        # Live paper mode must always run on fresh market data.
        # Persisted strategy snapshots may contain static candles from past backtests.
        config_without_candles = dict(config)
        config_without_candles.pop("candles", None)
        payload: dict[str, Any] = model_cls(**config_without_candles).model_dump()

        total_balance = float(profile.total_balance_usdt)
        per_trade = float(profile.per_trade_usdt)

        if strategy_type == "builder_vwap":
            payload["account_balance"] = total_balance
            pct = (per_trade / total_balance) * 100.0 if total_balance > 0 else 100.0
            payload["max_position_pct"] = min(100.0, max(0.01, pct))
        elif strategy_type == "atr_order_block":
            payload["allocation_usdt"] = per_trade
        elif strategy_type == "knife_catcher":
            payload["account_balance"] = total_balance
        elif strategy_type == "grid_bot":
            payload["initial_capital_usdt"] = total_balance
            payload["order_size_usdt"] = per_trade
        elif strategy_type == "intraday_momentum":
            payload["allocation_usdt"] = total_balance
            payload["entry_size_usdt"] = per_trade
        return payload

    async def _build_metrics(
        self,
        *,
        session: AsyncSession,
        profile: LivePaperProfile,
    ) -> dict[str, Any]:
        current_initial_balance = await self._resolve_current_initial_balance(session=session, profile=profile)
        stats_start_time = await self._resolve_stats_start_time(session=session, profile=profile)
        base_filters = [
            LivePaperTrade.profile_id == profile.id,
            LivePaperTrade.strategy_revision == profile.strategy_revision,
        ]
        if stats_start_time is not None:
            base_filters.append(LivePaperTrade.exit_time > stats_start_time)

        sum_stmt = select(func.coalesce(func.sum(LivePaperTrade.pnl_usdt), 0.0)).where(*base_filters)
        total_pnl = float((await session.scalar(sum_stmt)) or 0.0)
        count_stmt = select(func.count(LivePaperTrade.id)).where(*base_filters)
        total_trades = int((await session.scalar(count_stmt)) or 0)
        rows = (
            await session.scalars(
                select(LivePaperTrade)
                .where(*base_filters)
                .order_by(LivePaperTrade.exit_time.asc(), LivePaperTrade.id.asc())
            )
        ).all()
        equity = float(current_initial_balance)
        equity_curve: list[dict[str, Any]] = [
            {"step": 0, "time": None, "equity": equity, "pnl_usdt": 0.0}
        ]
        for idx, row in enumerate(rows, start=1):
            equity += float(row.pnl_usdt)
            equity_curve.append(
                {
                    "step": idx,
                    "time": row.exit_time.isoformat(),
                    "equity": float(equity),
                    "pnl_usdt": float(row.pnl_usdt),
                }
            )
        return {
            "initial_balance": float(current_initial_balance),
            "current_balance": float(current_initial_balance + total_pnl),
            "total_pnl": total_pnl,
            "closed_trades": total_trades,
            "equity_curve": equity_curve,
        }

    async def _resolve_current_initial_balance(
        self,
        *,
        session: AsyncSession,
        profile: LivePaperProfile,
    ) -> float:
        started_event = await session.scalar(
            select(LivePaperEvent)
            .where(
                LivePaperEvent.profile_id == profile.id,
                LivePaperEvent.strategy_revision == profile.strategy_revision,
                LivePaperEvent.event_type == "paper_started",
            )
            .order_by(desc(LivePaperEvent.id))
            .limit(1)
        )
        if started_event is not None:
            started_payload = started_event.payload if isinstance(started_event.payload, dict) else {}
            started_balance = started_payload.get("total_balance_usdt")
            try:
                if started_balance is not None:
                    return float(started_balance)
            except (TypeError, ValueError):
                return float(profile.total_balance_usdt)
        if profile.strategy_revision == 1:
            return float(profile.total_balance_usdt)
        row = await session.scalar(
            select(LivePaperEvent)
            .where(
                LivePaperEvent.profile_id == profile.id,
                LivePaperEvent.strategy_revision == profile.strategy_revision,
                LivePaperEvent.event_type == "strategy_switched",
            )
            .order_by(desc(LivePaperEvent.id))
            .limit(1)
        )
        if row is None:
            return float(profile.total_balance_usdt)
        payload = row.payload if isinstance(row.payload, dict) else {}
        to_total_balance = payload.get("to_total_balance_usdt")
        try:
            if to_total_balance is not None:
                return float(to_total_balance)
        except (TypeError, ValueError):
            return float(profile.total_balance_usdt)
        return float(profile.total_balance_usdt)

    async def _resolve_stats_start_time(
        self,
        *,
        session: AsyncSession,
        profile: LivePaperProfile,
    ) -> datetime | None:
        started_at = await session.scalar(
            select(func.max(LivePaperEvent.event_time)).where(
                LivePaperEvent.profile_id == profile.id,
                LivePaperEvent.strategy_revision == profile.strategy_revision,
                LivePaperEvent.event_type == "paper_started",
            )
        )
        if started_at is None:
            return None
        return _normalize_time(started_at)

    async def _build_revision_snapshot(
        self,
        *,
        session: AsyncSession,
        profile_id: int,
        strategy_revision: int,
        initial_balance: float,
    ) -> dict[str, float | int]:
        pnl_sum = await session.scalar(
            select(func.coalesce(func.sum(LivePaperTrade.pnl_usdt), 0.0)).where(
                LivePaperTrade.profile_id == profile_id,
                LivePaperTrade.strategy_revision == strategy_revision,
            )
        )
        trade_count = await session.scalar(
            select(func.count(LivePaperTrade.id)).where(
                LivePaperTrade.profile_id == profile_id,
                LivePaperTrade.strategy_revision == strategy_revision,
            )
        )
        realized_pnl = float(pnl_sum or 0.0)
        closed_trades = int(trade_count or 0)
        return {
            "initial_balance": float(initial_balance),
            "realized_pnl": realized_pnl,
            "current_balance": float(initial_balance + realized_pnl),
            "closed_trades": closed_trades,
        }

    async def _get_profile(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        for_update: bool,
    ) -> LivePaperProfile | None:
        stmt = select(LivePaperProfile).where(LivePaperProfile.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    async def _get_user_strategy(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        strategy_id: int,
    ) -> Strategy:
        row = await session.scalar(
            select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id)
        )
        if row is None:
            raise LookupError("Strategy not found.")
        return row


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_sanitize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_sanitize(item) for item in value]
    # Handles numpy scalars and other custom objects.
    if hasattr(value, "item"):
        try:
            return _json_sanitize(value.item())
        except Exception:
            return str(value)
    return str(value)
