from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.exchange_trading import (
    AccountAutoTradeEventRead,
    AccountTradeRead,
    AccountTradesPnlRead,
    AccountTradesRead,
    AccountTradesSyncStateRead,
)
from app.services.auto_trade.signal import to_linear_perp_symbol
from app.services.auto_trade.service import AutoTradeService
from app.services.auto_trade.trade_sync import ExchangeTradeSyncService
from app.services.exchange_credentials.service import ExchangeCredentialsService
from app.services.execution.futures_pnl import calculate_futures_pnl_fifo
from app.services.execution.trading_service import TradingService


class AccountTradesService:
    def __init__(
        self,
        *,
        sync_service: ExchangeTradeSyncService | None = None,
        trading_service: TradingService | None = None,
        auto_trade_service: AutoTradeService | None = None,
    ) -> None:
        self._credentials = ExchangeCredentialsService()
        self._sync = sync_service or ExchangeTradeSyncService()
        self._trading = trading_service or TradingService()
        self._auto_trade = auto_trade_service or AutoTradeService()

    async def get_account_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        limit: int,
        events_limit: int,
    ) -> AccountTradesRead:
        await self._credentials.get_account(session=session, account_id=account_id, user_id=user_id)
        normalized_symbol = to_linear_perp_symbol(symbol)
        sync_result = await self._sync.sync_account_symbol_trades(
            session=session,
            user_id=user_id,
            account_id=account_id,
            symbol=normalized_symbol,
            market_type="futures",
        )
        rows = await self._sync.list_trades(
            session=session,
            user_id=user_id,
            account_id=account_id,
            symbol=normalized_symbol,
            origin=None,
            limit=limit,
        )
        state = await self._sync.get_sync_state(
            session=session,
            user_id=user_id,
            account_id=account_id,
            symbol=normalized_symbol,
            market_type="futures",
        )
        warnings = list(sync_result.warnings)
        live_position = None
        try:
            live_position = await self._trading.fetch_futures_position(
                session=session,
                user_id=user_id,
                account_id=account_id,
                symbol=normalized_symbol,
            )
        except Exception as exc:
            warnings.append(f"futures_position_unavailable: {exc}")

        pnl = calculate_futures_pnl_fifo(
            symbol=normalized_symbol, trades=rows, live_position=live_position
        )
        events = await self._auto_trade.list_events(
            session=session,
            user_id=user_id,
            limit=events_limit,
            account_id=account_id,
        )

        return AccountTradesRead(
            account_id=account_id,
            symbol=normalized_symbol,
            trades=[
                AccountTradeRead(
                    exchange_trade_id=row.exchange_trade_id,
                    timestamp=self._ensure_utc(row.traded_at),
                    side=row.side,
                    price=float(row.price),
                    amount=float(row.amount),
                    fee=float(row.fee_cost),
                    fee_currency=row.fee_currency,
                    order_id=row.exchange_order_id,
                    is_autotrade=row.origin == "platform",
                    raw=row.raw_trade if isinstance(row.raw_trade, dict) else {},
                )
                for row in rows
            ],
            pnl=AccountTradesPnlRead(
                realized=float(pnl.realized),
                unrealized=float(pnl.unrealized),
                base_currency=pnl.base_currency,
                quote_currency=pnl.quote_currency,
            ),
            sync_state=AccountTradesSyncStateRead(
                last_trade_id=(state.last_trade_id if state is not None else None),
                last_trade_ts=(
                    datetime.fromtimestamp(state.last_trade_ts_ms / 1000.0, tz=UTC)
                    if state is not None and state.last_trade_ts_ms > 0
                    else None
                ),
            ),
            auto_trade_events=[
                AccountAutoTradeEventRead(
                    id=item.id,
                    event_type=item.event_type,
                    level=item.level,
                    message=item.message,
                    created_at=self._ensure_utc(item.created_at),
                    payload=item.payload if isinstance(item.payload, dict) else {},
                )
                for item in events
            ],
            sync_warnings=warnings,
        )

    @staticmethod
    def _ensure_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
