"""Exchange adapter abstractions for unified exchange integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional


class OrderSide(str, Enum):
    """Order direction for exchange order placement."""

    BUY = "buy"
    SELL = "sell"


class PositionSide(str, Enum):
    """Position side for one-way and hedge modes."""

    LONG = "long"
    SHORT = "short"
    BOTH = "both"  # One-way mode


@dataclass
class ConditionalOrderResult:
    """Normalized conditional order result from an exchange."""

    exchange_order_id: str  # Binance: algoId, Bybit: orderId
    client_order_id: str
    order_type: str  # "stop_loss" | "take_profit" | "trailing_stop"
    trigger_price: float
    quantity: float
    status: str  # "new" | "triggered" | "cancelled" | "rejected"
    is_algo: bool = False  # True for Binance algo orders


@dataclass
class PartialCloseResult:
    """Result of a partial position close."""

    executed_qty: float
    avg_price: float
    remaining_qty: float
    order_id: str
    commission: float


@dataclass
class EntryOrderResult:
    """Normalized entry-order result for opening a position."""

    exchange_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: str
    status: str
    quantity: float
    filled_quantity: float
    remaining_quantity: float
    price: float | None
    average_price: float | None
    cost: float | None
    timestamp: datetime | None
    raw: dict[str, Any]
    attached_sl: ConditionalOrderResult | None = None
    attached_tp: ConditionalOrderResult | None = None


@dataclass
class PositionSnapshot:
    """Exchange-side position state for reconciliation."""

    symbol: str
    side: PositionSide
    size: float  # Absolute qty
    entry_price: float
    unrealized_pnl: float
    leverage: int
    mark_price: float
    liquidation_price: float
    open_orders: list[ConditionalOrderResult]  # Active SL/TP on exchange


@dataclass
class RateLimitState:
    """Current account/IP rate limit counters and limits."""

    order_count_10s: int
    order_count_1m: int
    order_limit_10s: int  # Binance: varies by VIP level
    order_limit_1m: int
    weight_used_1m: int
    weight_limit_1m: int
    retry_after: Optional[float]  # seconds until rate limit resets


class ExchangeAdapter(ABC):
    """
    Unified interface for exchange interactions.
    Each method handles exchange-specific quirks internally.
    """

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[PositionSnapshot]:
        """Fetch current position state from exchange."""
        raise NotImplementedError

    @abstractmethod
    async def get_open_conditional_orders(
        self,
        symbol: str,
    ) -> list[ConditionalOrderResult]:
        """Fetch all active SL/TP/trailing orders for symbol."""
        raise NotImplementedError

    @abstractmethod
    async def place_entry_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        client_order_id: str,
        *,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
        sl_client_order_id: str | None = None,
        tp_client_order_id: str | None = None,
    ) -> EntryOrderResult:
        """
        Place a new non-reduce-only market entry order, optionally attaching
        bracket protective orders (stop-loss and/or take-profit).

        When ``take_profit_price`` or ``stop_loss_price`` is provided the adapter
        guarantees that the position is opened with the requested protection in
        place (Bybit attaches via native trading-stop fields in the same request;
        Binance places the entry first and then conditional algo orders, rolling
        back the entry if any protective placement fails). On success the
        ``attached_sl`` / ``attached_tp`` fields are populated.
        """
        raise NotImplementedError

    @abstractmethod
    async def place_stop_loss(
        self,
        symbol: str,
        side: OrderSide,  # SELL for long SL, BUY for short SL
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        reduce_only: bool = True,
    ) -> ConditionalOrderResult:
        """
        Place a stop-loss order.
        Binance: POST /fapi/v1/algoOrder (STOP_MARKET)
        Bybit:   POST /v5/position/trading-stop (slSize, slTriggerBy)
        """
        raise NotImplementedError

    @abstractmethod
    async def place_take_profit(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        reduce_only: bool = True,
        limit_price: Optional[float] = None,  # None = market TP
    ) -> ConditionalOrderResult:
        """
        Place a take-profit order.
        Binance: POST /fapi/v1/algoOrder (TAKE_PROFIT_MARKET)
        Bybit:   POST /v5/position/trading-stop (tpSize, tpslMode=Partial)
        """
        raise NotImplementedError

    @abstractmethod
    async def place_trailing_stop(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        callback_rate: float,  # % distance from peak
        activation_price: Optional[float],
        client_order_id: str,
    ) -> ConditionalOrderResult:
        """
        Place a trailing-stop order.
        Binance: algoOrder with TRAILING_STOP_MARKET
        Bybit:   /v5/position/trading-stop with trailingStop param
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel_and_replace_sl(
        self,
        symbol: str,
        existing_order_id: str,
        new_trigger_price: float,
        new_quantity: float,
        client_order_id: str,
    ) -> ConditionalOrderResult:
        """
        Atomically cancel existing SL and place new one.

        Binance: DELETE /fapi/v1/algoOrder -> POST /fapi/v1/algoOrder
                 (no native amend for algo orders)
        Bybit:   POST /v5/position/trading-stop (modifies in-place)
                 OR /v5/order/amend for conditional orders

        CRITICAL: If cancel succeeds but new placement fails,
        must retry placement with exponential backoff.
        Position is UNPROTECTED until new SL is placed.
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel_and_replace_tp(
        self,
        symbol: str,
        existing_order_id: str,
        new_trigger_price: float,
        new_quantity: float,
        client_order_id: str,
        limit_price: Optional[float] = None,
    ) -> ConditionalOrderResult:
        """Same pattern as SL but for TP orders."""
        raise NotImplementedError

    @abstractmethod
    async def cancel_conditional_order(
        self,
        symbol: str,
        order_id: str,
    ) -> bool:
        """Cancel active conditional order by exchange order id."""
        raise NotImplementedError

    @abstractmethod
    async def partial_close(
        self,
        symbol: str,
        side: OrderSide,  # Opposite to position side
        quantity: float,
        client_order_id: str,
        order_type: str = "market",  # "market" | "limit"
        price: Optional[float] = None,
    ) -> PartialCloseResult:
        """
        Reduce position by given quantity.
        Uses reduce_only=True to prevent flipping.
        """
        raise NotImplementedError

    @abstractmethod
    async def subscribe_user_data(
        self,
        on_order_update: Callable[..., Any],  # ORDER_TRADE_UPDATE / ALGO_UPDATE
        on_position_update: Callable[..., Any],  # ACCOUNT_UPDATE
        on_disconnect: Callable[..., Any],
    ) -> None:
        """
        Start WebSocket user data stream.
        Binance: listenKey -> wss://fstream.binance.com/ws/<listenKey>
                 Events: ORDER_TRADE_UPDATE, ALGO_UPDATE, ACCOUNT_UPDATE
        Bybit:   wss://stream.bybit.com/v5/private
                 Topics: order, execution, position
        """
        raise NotImplementedError

    @abstractmethod
    async def subscribe_kline(
        self,
        symbol: str,
        interval: str,  # "1m", "5m", "15m", "1h"
        on_kline: Callable[..., Any],
    ) -> None:
        """Subscribe to kline stream for indicator calculations."""
        raise NotImplementedError

    @abstractmethod
    async def get_rate_limit_state(self) -> RateLimitState:
        """Return current rate limit consumption."""
        raise NotImplementedError

    @abstractmethod
    async def can_place_order(self) -> bool:
        """Check if we have rate limit headroom for an order."""
        raise NotImplementedError
