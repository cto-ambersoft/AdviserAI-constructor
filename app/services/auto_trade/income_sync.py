"""Sync Binance funding/income rows into ``exchange_income_ledger``.

Mirrors :class:`ExchangeTradeSyncService` but for ``/fapi/v1/income``
(FUNDING_FEE only). Funding settles every 8h and is sparse, so the cursor is
derived from ``MAX(income_at)`` per (account, symbol) rather than a separate
state row, and a small overlap window plus idempotent upsert on
(account_id, exchange_name, income_type, tran_id) handles boundary dedup.

Binance-only for now: non-Binance configs are a no-op (their funding is reported
through different endpoints).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auto_trade_config import AutoTradeConfig
from app.models.exchange import ExchangeCredential
from app.models.exchange_income_ledger import ExchangeIncomeLedger
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.services.execution.trading_service import TradingService

from .signal import to_linear_perp_symbol

FUNDING_FEE = "FUNDING_FEE"
MARKET_TYPE_FUTURES = "futures"
_SYNC_OVERLAP = timedelta(minutes=5)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def sum_funding(
    *,
    session: AsyncSession,
    account_id: int,
    symbol: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> float:
    """Signed sum of FUNDING_FEE income for (account, symbol) within ``[start, end)``.

    Positive is funding received, negative is funding paid. The window is
    inclusive-lower / exclusive-upper so a position window ``[opened_at,
    closed_at)`` attributes each funding event to exactly one position. Returns
    ``0.0`` when there are no matching rows. Account+symbol scoping is exact;
    per-position attribution is exact for the common sequential-position case and
    an approximation only when same-symbol positions overlap in time.
    """
    stmt = select(func.coalesce(func.sum(ExchangeIncomeLedger.income), 0.0)).where(
        ExchangeIncomeLedger.account_id == account_id,
        ExchangeIncomeLedger.symbol == symbol,
        ExchangeIncomeLedger.income_type == FUNDING_FEE,
    )
    if start is not None:
        stmt = stmt.where(ExchangeIncomeLedger.income_at >= start)
    if end is not None:
        stmt = stmt.where(ExchangeIncomeLedger.income_at < end)
    total = await session.scalar(stmt)
    return float(total or 0.0)


class ExchangeIncomeSyncService:
    def __init__(self, trading_service: TradingService | None = None) -> None:
        self._trading = trading_service or TradingService()
        self._backfill_days = 30
        self._page_limit = 1000

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
                inserted = await self.sync_config_income(session=session, config=config)
            except Exception:
                await session.rollback()
                stats["errors"] += 1
                continue
            stats["synced"] += 1
            stats["inserted_or_updated"] += inserted
        return stats

    async def sync_config_income(
        self, *, session: AsyncSession, config: AutoTradeConfig
    ) -> int:
        exchange_name = await self._exchange_name(session=session, account_id=config.account_id)
        if exchange_name != "binance":
            return 0
        profile = await session.scalar(
            select(PersonalAnalysisProfile).where(
                PersonalAnalysisProfile.id == config.profile_id,
                PersonalAnalysisProfile.user_id == config.user_id,
            )
        )
        if profile is None:
            return 0
        symbol = to_linear_perp_symbol(profile.symbol)
        return await self.sync_symbol_income(
            session=session,
            user_id=config.user_id,
            account_id=config.account_id,
            symbol=symbol,
            exchange_name=exchange_name,
        )

    async def sync_symbol_income(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        exchange_name: str,
    ) -> int:
        since = await self._compute_since(session=session, account_id=account_id, symbol=symbol)
        inserted = 0
        for _ in range(10):
            rows = await self._trading.fetch_futures_income(
                session=session,
                user_id=user_id,
                account_id=account_id,
                symbol=symbol,
                since=since,
                limit=self._page_limit,
            )
            if not rows:
                break
            inserted += await self._upsert_income_rows(
                session=session,
                user_id=user_id,
                account_id=account_id,
                exchange_name=exchange_name,
                symbol=symbol,
                rows=rows,
            )
            timestamps = [_to_utc(row.timestamp) for row in rows if row.timestamp is not None]
            if not timestamps or len(rows) < self._page_limit:
                break
            max_ts = max(timestamps)
            if since is not None and max_ts <= since:
                break
            since = max_ts + timedelta(milliseconds=1)
        await session.commit()
        return inserted

    async def _compute_since(
        self, *, session: AsyncSession, account_id: int, symbol: str
    ) -> datetime:
        last = await session.scalar(
            select(func.max(ExchangeIncomeLedger.income_at)).where(
                ExchangeIncomeLedger.account_id == account_id,
                ExchangeIncomeLedger.symbol == symbol,
                ExchangeIncomeLedger.income_type == FUNDING_FEE,
            )
        )
        if last is None:
            return _utc_now() - timedelta(days=self._backfill_days)
        return _to_utc(last) - _SYNC_OVERLAP

    async def _upsert_income_rows(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        exchange_name: str,
        symbol: str,
        rows: list[Any],
    ) -> int:
        now = _utc_now()
        values: list[dict[str, Any]] = []
        for income in rows:
            if not income.tran_id:
                continue
            income_at = _to_utc(income.timestamp) if income.timestamp is not None else now
            values.append(
                {
                    "user_id": user_id,
                    "account_id": account_id,
                    "exchange_name": exchange_name,
                    "market_type": MARKET_TYPE_FUTURES,
                    "income_type": income.income_type,
                    "asset": income.asset,
                    "income": float(income.income),
                    "symbol": income.symbol or symbol,
                    "tran_id": income.tran_id,
                    "trade_id": income.trade_id,
                    "info": income.info,
                    "income_at": income_at,
                    "ingested_at": now,
                    "raw": income.raw if isinstance(income.raw, dict) else {},
                }
            )
        if not values:
            return 0
        update_columns = {
            "asset",
            "income",
            "symbol",
            "trade_id",
            "info",
            "income_at",
            "ingested_at",
            "raw",
        }
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            stmt = sqlite_insert(ExchangeIncomeLedger).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["account_id", "exchange_name", "income_type", "tran_id"],
                set_={column: stmt.excluded[column] for column in update_columns},
            )
        else:
            stmt = pg_insert(ExchangeIncomeLedger).values(values)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_exchange_income_ledger_identity",
                set_={column: stmt.excluded[column] for column in update_columns},
            )
        await session.execute(stmt)
        return len(values)

    async def _exchange_name(self, *, session: AsyncSession, account_id: int) -> str:
        row = await session.scalar(
            select(ExchangeCredential.exchange_name).where(ExchangeCredential.id == account_id)
        )
        return str(row or "unknown")
