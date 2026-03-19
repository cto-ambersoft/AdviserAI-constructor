from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, desc, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.exchange import ExchangeCredential
from app.models.exchange_order_metadata import ExchangeOrderMetadata
from app.models.exchange_trade_ledger import ExchangeTradeLedger
from app.models.exchange_trade_sync_state import ExchangeTradeSyncState
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.schemas.exchange_trading import NormalizedTrade
from app.services.execution.errors import ExchangeServiceError
from app.services.execution.trading_service import TradingService

from .signal import to_linear_perp_symbol

MARKET_TYPE_FUTURES = "futures"
_SYNC_OVERLAP_MS = 2 * 60 * 1000


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(slots=True)
class _OriginResolution:
    origin: str
    confidence: str
    config_id: int | None
    position_id: int | None
    open_history_id: int | None
    close_history_id: int | None


@dataclass(slots=True)
class TradeSyncResult:
    inserted_or_updated: int
    warnings: list[str]
    last_trade_id: str | None
    last_trade_ts: datetime | None


class ExchangeTradeSyncService:
    def __init__(self, trading_service: TradingService | None = None) -> None:
        self._trading = trading_service or TradingService()
        settings = get_settings()
        self._page_limit = max(10, min(settings.exchange_default_page_limit, 200))
        self._backfill_days = 30

    async def sync_running_configs(self, *, session: AsyncSession) -> dict[str, int]:
        stats = {"configs": 0, "synced": 0, "inserted_or_updated": 0, "errors": 0}
        configs = list(
            (
                await session.scalars(
                    select(AutoTradeConfig).where(
                        AutoTradeConfig.enabled.is_(True),
                        AutoTradeConfig.is_running.is_(True),
                    )
                )
            ).all()
        )
        stats["configs"] = len(configs)
        for config in configs:
            try:
                inserted = await self.sync_config_trades(session=session, config=config)
            except Exception:
                await session.rollback()
                stats["errors"] += 1
                continue
            stats["synced"] += 1
            stats["inserted_or_updated"] += inserted
        return stats

    async def sync_config_trades(
        self,
        *,
        session: AsyncSession,
        config: AutoTradeConfig,
    ) -> int:
        profile = cast_profile(
            await session.scalar(
                select(PersonalAnalysisProfile).where(
                    PersonalAnalysisProfile.id == config.profile_id,
                    PersonalAnalysisProfile.user_id == config.user_id,
                )
            )
        )
        if profile is None:
            return 0
        symbol = to_linear_perp_symbol(profile.symbol)
        return await self.sync_symbol_trades(
            session=session,
            user_id=config.user_id,
            account_id=config.account_id,
            symbol=symbol,
            market_type=MARKET_TYPE_FUTURES,
            backfill_days=self._backfill_days,
        )

    async def sync_symbol_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        market_type: str,
        backfill_days: int,
    ) -> int:
        state = await self._get_or_create_sync_state(
            session=session,
            user_id=user_id,
            account_id=account_id,
            symbol=symbol,
            market_type=market_type,
        )
        now = _utc_now()
        if state.last_backfill_at is None:
            since_dt = now - timedelta(days=backfill_days)
            state.last_backfill_at = now
        elif state.last_trade_ts_ms > 0:
            since_ms = max(0, int(state.last_trade_ts_ms) - _SYNC_OVERLAP_MS)
            since_dt = datetime.fromtimestamp(since_ms / 1000.0, tz=UTC)
        else:
            since_dt = now - timedelta(hours=12)

        inserted_or_updated = 0
        cursor = state.last_trade_id
        max_ts_ms = int(state.last_trade_ts_ms)
        max_trade_id = state.last_trade_id
        for _ in range(20):
            trades, next_cursor = await self._trading.fetch_futures_trades_page(
                session=session,
                user_id=user_id,
                account_id=account_id,
                symbol=symbol,
                since=since_dt,
                limit=self._page_limit,
                cursor=cursor,
            )
            if not trades:
                break
            rows = []
            for trade in trades:
                traded_at = _to_utc(trade.timestamp) or now
                traded_ts_ms = int(traded_at.timestamp() * 1000)
                if traded_ts_ms > max_ts_ms:
                    max_ts_ms = traded_ts_ms
                if trade.id and (max_trade_id is None or trade.id > max_trade_id):
                    max_trade_id = trade.id
                origin = await self._resolve_origin(
                    session=session,
                    account_id=account_id,
                    trade=trade,
                )
                rows.append(
                    {
                        "user_id": user_id,
                        "account_id": account_id,
                        "exchange_name": await self._exchange_name(
                            session=session,
                            account_id=account_id,
                        ),
                        "market_type": market_type,
                        "symbol": trade.symbol or symbol,
                        "exchange_trade_id": trade.id,
                        "exchange_order_id": trade.order_id,
                        "client_order_id": self._extract_client_order_id(trade=trade),
                        "side": trade.side,
                        "price": float(trade.price),
                        "amount": float(trade.amount),
                        "cost": float(trade.cost) if trade.cost is not None else None,
                        "fee_cost": float(trade.fee_cost),
                        "fee_currency": trade.fee_currency,
                        "traded_at": traded_at,
                        "ingested_at": now,
                        "origin": origin.origin,
                        "origin_confidence": origin.confidence,
                        "auto_trade_config_id": origin.config_id,
                        "auto_trade_position_id": origin.position_id,
                        "open_history_id": origin.open_history_id,
                        "close_history_id": origin.close_history_id,
                        "raw_trade": trade.raw if isinstance(trade.raw, dict) else {},
                    }
                )
            inserted_or_updated += await self._upsert_ledger_rows(session=session, rows=rows)
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor

        state.last_trade_ts_ms = max(state.last_trade_ts_ms, max_ts_ms)
        state.last_trade_id = max_trade_id
        state.last_sync_at = now
        state.error_count = 0
        await session.commit()
        return inserted_or_updated

    async def sync_account_symbol_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        market_type: str = MARKET_TYPE_FUTURES,
    ) -> TradeSyncResult:
        state = await self._get_or_create_sync_state(
            session=session,
            user_id=user_id,
            account_id=account_id,
            symbol=symbol,
            market_type=market_type,
        )
        now = _utc_now()
        warnings: list[str] = []
        if state.last_trade_ts_ms > 0:
            since_ms = max(0, int(state.last_trade_ts_ms) - _SYNC_OVERLAP_MS)
            since_dt = datetime.fromtimestamp(since_ms / 1000.0, tz=UTC)
            cursor = state.last_trade_id
            max_pages = 20
        else:
            # First request syncs full available history by paging from the exchange origin.
            since_dt = None
            cursor = None
            max_pages = 200

        inserted_or_updated = 0
        max_ts_ms = int(state.last_trade_ts_ms)
        max_trade_id = state.last_trade_id
        exchange_name = await self._exchange_name(session=session, account_id=account_id)
        try:
            for _ in range(max_pages):
                trades, next_cursor = await self._trading.fetch_futures_trades_page(
                    session=session,
                    user_id=user_id,
                    account_id=account_id,
                    symbol=symbol,
                    since=since_dt,
                    limit=self._page_limit,
                    cursor=cursor,
                )
                if not trades:
                    break
                rows = []
                for trade in trades:
                    if not trade.id:
                        continue
                    traded_at = _to_utc(trade.timestamp) or now
                    traded_ts_ms = int(traded_at.timestamp() * 1000)
                    if traded_ts_ms > max_ts_ms:
                        max_ts_ms = traded_ts_ms
                    if trade.id and (max_trade_id is None or trade.id > max_trade_id):
                        max_trade_id = trade.id
                    origin = await self._resolve_origin(
                        session=session,
                        account_id=account_id,
                        trade=trade,
                    )
                    rows.append(
                        {
                            "user_id": user_id,
                            "account_id": account_id,
                            "exchange_name": exchange_name,
                            "market_type": market_type,
                            "symbol": trade.symbol or symbol,
                            "exchange_trade_id": trade.id,
                            "exchange_order_id": trade.order_id,
                            "client_order_id": self._extract_client_order_id(trade=trade),
                            "side": trade.side,
                            "price": float(trade.price),
                            "amount": float(trade.amount),
                            "cost": float(trade.cost) if trade.cost is not None else None,
                            "fee_cost": float(trade.fee_cost),
                            "fee_currency": trade.fee_currency,
                            "traded_at": traded_at,
                            "ingested_at": now,
                            "origin": origin.origin,
                            "origin_confidence": origin.confidence,
                            "auto_trade_config_id": origin.config_id,
                            "auto_trade_position_id": origin.position_id,
                            "open_history_id": origin.open_history_id,
                            "close_history_id": origin.close_history_id,
                            "raw_trade": trade.raw if isinstance(trade.raw, dict) else {},
                        }
                    )
                inserted_or_updated += await self._upsert_ledger_rows(session=session, rows=rows)
                if next_cursor is None or next_cursor == cursor:
                    break
                cursor = next_cursor
        except ExchangeServiceError as exc:
            state.error_count = int(state.error_count) + 1
            state.last_sync_at = now
            await session.commit()
            warnings.append(f"{exc.code}: {exc.message}")
            return TradeSyncResult(
                inserted_or_updated=inserted_or_updated,
                warnings=warnings,
                last_trade_id=state.last_trade_id,
                last_trade_ts=(
                    datetime.fromtimestamp(state.last_trade_ts_ms / 1000.0, tz=UTC)
                    if state.last_trade_ts_ms > 0
                    else None
                ),
            )

        state.last_trade_ts_ms = max(state.last_trade_ts_ms, max_ts_ms)
        state.last_trade_id = max_trade_id
        state.last_sync_at = now
        state.error_count = 0
        await session.commit()
        return TradeSyncResult(
            inserted_or_updated=inserted_or_updated,
            warnings=warnings,
            last_trade_id=state.last_trade_id,
            last_trade_ts=(
                datetime.fromtimestamp(state.last_trade_ts_ms / 1000.0, tz=UTC)
                if state.last_trade_ts_ms > 0
                else None
            ),
        )

    async def list_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str | None = None,
        origin: str | None = None,
        limit: int = 100,
    ) -> list[ExchangeTradeLedger]:
        stmt: Select[tuple[ExchangeTradeLedger]] = (
            select(ExchangeTradeLedger)
            .where(
                ExchangeTradeLedger.user_id == user_id,
                ExchangeTradeLedger.account_id == account_id,
                ExchangeTradeLedger.market_type == MARKET_TYPE_FUTURES,
            )
            .order_by(desc(ExchangeTradeLedger.traded_at), desc(ExchangeTradeLedger.id))
            .limit(limit)
        )
        if symbol is not None:
            stmt = stmt.where(ExchangeTradeLedger.symbol == symbol)
        if origin is not None:
            stmt = stmt.where(ExchangeTradeLedger.origin == origin)
        return list((await session.scalars(stmt)).all())

    async def get_sync_state(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        market_type: str = MARKET_TYPE_FUTURES,
    ) -> ExchangeTradeSyncState | None:
        stmt: Select[tuple[ExchangeTradeSyncState]] = (
            select(ExchangeTradeSyncState)
            .where(
                ExchangeTradeSyncState.user_id == user_id,
                ExchangeTradeSyncState.account_id == account_id,
                ExchangeTradeSyncState.symbol == symbol,
                ExchangeTradeSyncState.market_type == market_type,
            )
            .limit(1)
        )
        return cast_sync_state(await session.scalar(stmt))

    async def _get_or_create_sync_state(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        market_type: str,
    ) -> ExchangeTradeSyncState:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        values = {
            "user_id": user_id,
            "account_id": account_id,
            "symbol": symbol,
            "market_type": market_type,
            "last_trade_ts_ms": 0,
            "last_trade_id": None,
            "last_sync_at": None,
            "last_backfill_at": None,
            "error_count": 0,
        }
        if dialect_name == "sqlite":
            insert_stmt = sqlite_insert(ExchangeTradeSyncState).values(values)
            insert_stmt = insert_stmt.on_conflict_do_nothing(
                index_elements=["account_id", "symbol", "market_type"]
            )
            await session.execute(insert_stmt)
        else:
            insert_stmt = pg_insert(ExchangeTradeSyncState).values(values)
            insert_stmt = insert_stmt.on_conflict_do_nothing(
                constraint="uq_exchange_trade_sync_state_scope"
            )
            await session.execute(insert_stmt)

        stmt: Select[tuple[ExchangeTradeSyncState]] = (
            select(ExchangeTradeSyncState)
            .where(
                ExchangeTradeSyncState.account_id == account_id,
                ExchangeTradeSyncState.symbol == symbol,
                ExchangeTradeSyncState.market_type == market_type,
            )
            .limit(1)
        )
        row = await session.scalar(stmt)
        if row is None:
            raise RuntimeError("Failed to resolve exchange trade sync state.")
        return row

    async def _upsert_ledger_rows(
        self,
        *,
        session: AsyncSession,
        rows: list[dict[str, Any]],
    ) -> int:
        if not rows:
            return 0
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        update_columns = {
            "exchange_order_id",
            "client_order_id",
            "cost",
            "fee_cost",
            "fee_currency",
            "origin",
            "origin_confidence",
            "auto_trade_config_id",
            "auto_trade_position_id",
            "open_history_id",
            "close_history_id",
            "raw_trade",
            "ingested_at",
        }
        if dialect_name == "sqlite":
            stmt = sqlite_insert(ExchangeTradeLedger).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    "account_id",
                    "exchange_name",
                    "market_type",
                    "symbol",
                    "exchange_trade_id",
                ],
                set_={column: stmt.excluded[column] for column in update_columns},
            )
            await session.execute(stmt)
            return len(rows)
        stmt = pg_insert(ExchangeTradeLedger).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_exchange_trade_ledger_trade_identity",
            set_={column: stmt.excluded[column] for column in update_columns},
        )
        await session.execute(stmt)
        return len(rows)

    async def _resolve_origin(
        self,
        *,
        session: AsyncSession,
        account_id: int,
        trade: NormalizedTrade,
    ) -> _OriginResolution:
        client_order_id = self._extract_client_order_id(trade=trade)
        order_id = trade.order_id
        if order_id or client_order_id:
            meta = cast_meta(
                await session.scalar(
                    select(ExchangeOrderMetadata)
                    .where(
                        ExchangeOrderMetadata.account_id == account_id,
                        or_(
                            and_(
                                ExchangeOrderMetadata.exchange_order_id.is_not(None),
                                ExchangeOrderMetadata.exchange_order_id == order_id,
                            ),
                            and_(
                                ExchangeOrderMetadata.client_order_id.is_not(None),
                                ExchangeOrderMetadata.client_order_id == client_order_id,
                            ),
                        ),
                    )
                    .order_by(ExchangeOrderMetadata.id.desc())
                    .limit(1)
                )
            )
            if meta is not None:
                return _OriginResolution(
                    origin="platform" if meta.source.startswith("auto_trade_") else "external",
                    confidence="strong",
                    config_id=meta.config_id,
                    position_id=meta.position_id,
                    open_history_id=meta.history_id if meta.source == "auto_trade_open" else None,
                    close_history_id=meta.history_id if meta.source == "auto_trade_close" else None,
                )

        position = cast_position(
            await session.scalar(
                select(AutoTradePosition)
                .where(
                    AutoTradePosition.account_id == account_id,
                    or_(
                        and_(
                            AutoTradePosition.open_order_id.is_not(None),
                            AutoTradePosition.open_order_id == order_id,
                        ),
                        and_(
                            AutoTradePosition.close_order_id.is_not(None),
                            AutoTradePosition.close_order_id == order_id,
                        ),
                        and_(
                            AutoTradePosition.open_order_id.is_not(None),
                            AutoTradePosition.open_order_id == client_order_id,
                        ),
                        and_(
                            AutoTradePosition.close_order_id.is_not(None),
                            AutoTradePosition.close_order_id == client_order_id,
                        ),
                    ),
                )
                .order_by(AutoTradePosition.id.desc())
                .limit(1)
            )
        )
        if position is not None:
            return _OriginResolution(
                origin="platform",
                confidence="weak",
                config_id=position.config_id,
                position_id=position.id,
                open_history_id=position.open_history_id,
                close_history_id=position.close_history_id,
            )
        return _OriginResolution(
            origin="external",
            confidence="none",
            config_id=None,
            position_id=None,
            open_history_id=None,
            close_history_id=None,
        )

    async def _exchange_name(self, *, session: AsyncSession, account_id: int) -> str:
        row = await session.scalar(
            select(ExchangeCredential.exchange_name).where(ExchangeCredential.id == account_id)
        )
        return str(row or "unknown")

    @staticmethod
    def _extract_client_order_id(*, trade: NormalizedTrade) -> str | None:
        raw = trade.raw
        if not isinstance(raw, dict):
            return None
        direct = raw.get("clientOrderId") or raw.get("orderLinkId")
        if isinstance(direct, str) and direct:
            return direct
        info = raw.get("info")
        if isinstance(info, dict):
            for key in ("clientOrderId", "orderLinkId", "origClientOrderId"):
                value = info.get(key)
                if isinstance(value, str) and value:
                    return value
        return None


def cast_profile(value: object) -> PersonalAnalysisProfile | None:
    if isinstance(value, PersonalAnalysisProfile):
        return value
    return None


def cast_meta(value: object) -> ExchangeOrderMetadata | None:
    if isinstance(value, ExchangeOrderMetadata):
        return value
    return None


def cast_position(value: object) -> AutoTradePosition | None:
    if isinstance(value, AutoTradePosition):
        return value
    return None


def cast_sync_state(value: object) -> ExchangeTradeSyncState | None:
    if isinstance(value, ExchangeTradeSyncState):
        return value
    return None
