from dataclasses import dataclass
from typing import Protocol

from app.schemas.exchange_trading import (
    AttachedTriggerOrder,
    ExchangeMode,
    ExchangeName,
    NormalizedBalance,
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

    async def fetch_spot_positions_view(
        self,
        *,
        quote_asset: str = "USDT",
    ) -> list[SpotPositionView]: ...

    async def fetch_ohlcv(
        self, *, symbol: str, timeframe: str, bars: int
    ) -> list[list[object]]: ...
