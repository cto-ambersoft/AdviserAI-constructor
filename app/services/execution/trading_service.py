from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.exchange_trading import (
    NormalizedFuturesPosition,
    NormalizedTrade,
    OrderSide,
    SpotBalancesRead,
    SpotOrderCreate,
    SpotOrderRead,
    SpotOrdersRead,
    SpotPnlRead,
    SpotPositionsRead,
    SpotTradesRead,
)
from app.services.exchange_credentials.service import ExchangeCredentialsService
from app.services.execution.factory import create_cex_adapter
from app.services.execution.pnl import calculate_spot_pnl


class TradingService:
    def __init__(self) -> None:
        self._credentials_service = ExchangeCredentialsService()

    async def place_spot_order(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        payload: SpotOrderCreate,
    ) -> SpotOrderRead:
        account = await self._credentials_service.get_account(session, payload.account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=payload.account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        order = await adapter.place_spot_order(
            symbol=payload.symbol,
            side=payload.side,
            order_type=payload.order_type,
            amount=payload.amount,
            price=payload.price,
            client_order_id=payload.client_order_id,
            attached_take_profit=payload.attached_take_profit,
            attached_stop_loss=payload.attached_stop_loss,
        )
        return SpotOrderRead(
            account_id=payload.account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            order=order,
        )

    async def cancel_spot_order(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        order_id: str,
        symbol: str | None = None,
    ) -> SpotOrderRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        order = await adapter.cancel_order(order_id=order_id, symbol=symbol)
        return SpotOrderRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            order=order,
        )

    async def get_spot_order_detail(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        order_id: str,
        symbol: str,
    ) -> SpotOrderRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        order = await adapter.fetch_order_detail(order_id=order_id, symbol=symbol)
        return SpotOrderRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            order=order,
        )

    async def get_spot_open_orders(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str | None,
        limit: int,
    ) -> SpotOrdersRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        orders = await adapter.fetch_open_orders(symbol=symbol, limit=limit)
        return SpotOrdersRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            orders=orders,
        )

    async def get_spot_order_history(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str | None,
        limit: int,
    ) -> SpotOrdersRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        orders = await adapter.fetch_closed_orders(symbol=symbol, limit=limit)
        return SpotOrdersRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            orders=orders,
        )

    async def get_spot_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str | None,
        limit: int,
    ) -> SpotTradesRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        trades = await adapter.fetch_trades(symbol=symbol, limit=limit)
        return SpotTradesRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            trades=trades,
        )

    async def get_spot_balances(
        self, *, session: AsyncSession, user_id: int, account_id: int
    ) -> SpotBalancesRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        balances = await adapter.fetch_balance()
        return SpotBalancesRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            balances=balances,
        )

    async def get_spot_positions(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        quote_asset: str,
    ) -> SpotPositionsRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        positions = await adapter.fetch_spot_positions_view(quote_asset=quote_asset)
        return SpotPositionsRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            positions=positions,
        )

    async def get_spot_pnl(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        quote_asset: str,
        limit: int,
    ) -> SpotPnlRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        balances = await adapter.fetch_balance()
        trades = await adapter.fetch_trades(symbol=None, limit=limit)
        positions = await adapter.fetch_spot_positions_view(quote_asset=quote_asset)
        mark_prices = {
            item.asset.upper(): float(item.mark_price)
            for item in positions
            if item.mark_price is not None and item.mark_price > 0
        }
        assets, realized, unrealized, fees = calculate_spot_pnl(
            trades=trades,
            balances=balances,
            quote_asset=quote_asset,
            mark_prices=mark_prices,
        )
        return SpotPnlRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            quote_asset=quote_asset.upper(),
            realized_pnl_quote=realized,
            unrealized_pnl_quote=unrealized,
            total_fees_quote=fees,
            assets=assets,
        )

    async def set_futures_leverage(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        leverage: int,
    ) -> None:
        await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        await adapter.set_futures_leverage(symbol=symbol, leverage=leverage)

    async def place_futures_market_order(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        side: OrderSide,
        amount: float,
        reduce_only: bool,
        client_order_id: str | None = None,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
    ) -> SpotOrderRead:
        account = await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        order = await adapter.place_futures_market_order(
            symbol=symbol,
            side=side,
            amount=amount,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
        )
        return SpotOrderRead(
            account_id=account_id,
            exchange_name=account.exchange_name,
            mode=account.mode,
            order=order,
        )

    async def close_futures_market_reduce_only(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        side: OrderSide,
        amount: float,
        client_order_id: str | None = None,
    ) -> SpotOrderRead:
        return await self.place_futures_market_order(
            session=session,
            user_id=user_id,
            account_id=account_id,
            symbol=symbol,
            side=side,
            amount=amount,
            reduce_only=True,
            client_order_id=client_order_id,
            take_profit_price=None,
            stop_loss_price=None,
        )

    async def fetch_futures_position(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
    ) -> NormalizedFuturesPosition | None:
        await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        return await adapter.fetch_futures_position(symbol=symbol)

    async def fetch_futures_trades(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[NormalizedTrade]:
        await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        return await adapter.fetch_futures_trades(symbol=symbol, since=since, limit=limit)

    async def fetch_futures_trades_page(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[NormalizedTrade], str | None]:
        await self._credentials_service.get_account(session, account_id, user_id)
        credentials = await self._credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        return await adapter.fetch_futures_trades_page(
            symbol=symbol,
            since=since,
            limit=limit,
            cursor=cursor,
        )
