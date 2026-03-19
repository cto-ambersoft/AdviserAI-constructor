from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.schemas.exchange_trading import (
    AttachedTriggerOrder,
    ExchangeMode,
    ExchangeName,
    NormalizedBalance,
    NormalizedFuturesPosition,
    NormalizedOrder,
    NormalizedTrade,
    OrderSide,
    OrderType,
    SpotPositionView,
)


@dataclass(slots=True)
class ExchangeCredentials:
    exchange_name: ExchangeName
    api_key: str
    api_secret: str
    mode: ExchangeMode
    passphrase: str | None = None


class CexAdapter(Protocol):
    async def ping(self) -> None: ...

    async def fetch_balance(self) -> list[NormalizedBalance]: ...

    async def place_spot_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: float,
        price: float | None = None,
        client_order_id: str | None = None,
        attached_take_profit: AttachedTriggerOrder | None = None,
        attached_stop_loss: AttachedTriggerOrder | None = None,
    ) -> NormalizedOrder: ...

    async def cancel_order(
        self, *, order_id: str, symbol: str | None = None
    ) -> NormalizedOrder: ...

    async def fetch_order_detail(self, *, order_id: str, symbol: str) -> NormalizedOrder: ...

    async def fetch_open_orders(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[NormalizedOrder]: ...

    async def fetch_closed_orders(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[NormalizedOrder]: ...

    async def fetch_trades(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[NormalizedTrade]: ...

    async def fetch_futures_trades(
        self,
        *,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[NormalizedTrade]: ...

    async def fetch_futures_trades_page(
        self,
        *,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[NormalizedTrade], str | None]: ...

    async def fetch_spot_positions_view(
        self,
        *,
        quote_asset: str = "USDT",
    ) -> list[SpotPositionView]: ...

    async def set_futures_leverage(self, *, symbol: str, leverage: int) -> None: ...

    async def place_futures_market_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        amount: float,
        reduce_only: bool = False,
        client_order_id: str | None = None,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
    ) -> NormalizedOrder: ...

    async def close_futures_market_reduce_only(
        self,
        *,
        symbol: str,
        side: OrderSide,
        amount: float,
        client_order_id: str | None = None,
    ) -> NormalizedOrder: ...

    async def fetch_futures_position(
        self,
        *,
        symbol: str,
    ) -> NormalizedFuturesPosition | None: ...

    async def fetch_ohlcv(
        self, *, symbol: str, timeframe: str, bars: int
    ) -> list[list[object]]: ...
