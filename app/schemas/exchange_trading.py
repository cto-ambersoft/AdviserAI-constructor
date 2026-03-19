from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ExchangeName = Literal["bybit", "binance", "okx"]
ExchangeMode = Literal["demo", "real"]
OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
OrderStatus = Literal["open", "closed", "canceled", "rejected", "expired", "unknown"]
FuturesPositionSide = Literal["long", "short", "flat"]

SUPPORTED_EXCHANGES: tuple[ExchangeName, ...] = ("bybit", "binance", "okx")
SUPPORTED_EXCHANGE_MODES: tuple[ExchangeMode, ...] = ("demo", "real")


class NormalizedBalance(BaseModel):
    asset: str
    free: float = Field(ge=0)
    used: float = Field(ge=0)
    total: float = Field(ge=0)


class NormalizedOrder(BaseModel):
    id: str
    client_order_id: str | None = None
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    amount: float = Field(ge=0)
    filled: float = Field(ge=0)
    remaining: float = Field(ge=0)
    price: float | None = None
    average: float | None = None
    cost: float | None = None
    timestamp: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class NormalizedTrade(BaseModel):
    id: str
    order_id: str | None = None
    symbol: str
    side: OrderSide
    amount: float = Field(ge=0)
    price: float = Field(ge=0)
    cost: float | None = None
    fee_cost: float = Field(ge=0, default=0.0)
    fee_currency: str | None = None
    timestamp: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class NormalizedFuturesPosition(BaseModel):
    symbol: str
    side: FuturesPositionSide
    contracts: float = Field(ge=0)
    entry_price: float | None = None
    mark_price: float | None = None
    leverage: float | None = None
    unrealized_pnl: float | None = None
    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    liquidation_price: float | None = None
    margin_mode: Literal["cross", "isolated"] | None = None
    notional: float | None = None
    collateral: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class SpotPositionView(BaseModel):
    asset: str
    quantity: float = Field(ge=0)
    mark_price: float | None = None
    market_value_quote: float | None = None
    unrealized_pnl_quote: float | None = None


class SpotOrderCreate(BaseModel):
    account_id: int = Field(ge=1)
    symbol: str = Field(min_length=3, max_length=32)
    side: OrderSide
    order_type: OrderType
    amount: float = Field(gt=0)
    price: float | None = Field(default=None, gt=0)
    client_order_id: str | None = Field(default=None, min_length=1, max_length=64)
    attached_take_profit: "AttachedTriggerOrder | None" = None
    attached_stop_loss: "AttachedTriggerOrder | None" = None

    @model_validator(mode="after")
    def validate_price_requirements(self) -> "SpotOrderCreate":
        if self.order_type == "limit" and self.price is None:
            raise ValueError("price is required for limit order.")
        return self


class AttachedTriggerOrder(BaseModel):
    trigger_price: float = Field(gt=0)
    order_type: OrderType = "market"
    price: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_limit_price(self) -> "AttachedTriggerOrder":
        if self.order_type == "limit" and self.price is None:
            raise ValueError("price is required when attached order_type is limit.")
        return self


class SpotOrderRead(BaseModel):
    account_id: int = Field(ge=1)
    exchange_name: str
    mode: str
    order: NormalizedOrder


class SpotOrdersRead(BaseModel):
    account_id: int = Field(ge=1)
    exchange_name: str
    mode: str
    orders: list[NormalizedOrder]


class SpotTradesRead(BaseModel):
    account_id: int = Field(ge=1)
    exchange_name: str
    mode: str
    trades: list[NormalizedTrade]


class SpotBalancesRead(BaseModel):
    account_id: int = Field(ge=1)
    exchange_name: str
    mode: str
    balances: list[NormalizedBalance]


class SpotPositionsRead(BaseModel):
    account_id: int = Field(ge=1)
    exchange_name: str
    mode: str
    positions: list[SpotPositionView]


class SpotPnlAsset(BaseModel):
    asset: str
    quantity: float = Field(ge=0)
    average_entry_price: float | None = None
    mark_price: float | None = None
    realized_pnl_quote: float = 0.0
    unrealized_pnl_quote: float = 0.0
    total_fees_quote: float = 0.0


class SpotPnlRead(BaseModel):
    account_id: int = Field(ge=1)
    exchange_name: str
    mode: str
    quote_asset: str
    realized_pnl_quote: float
    unrealized_pnl_quote: float
    total_fees_quote: float
    assets: list[SpotPnlAsset]


class AccountTradeRead(BaseModel):
    exchange_trade_id: str
    timestamp: datetime
    side: str
    price: float = Field(ge=0)
    amount: float = Field(ge=0)
    fee: float = Field(ge=0)
    fee_currency: str | None = None
    order_id: str | None = None
    is_autotrade: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class AccountTradesPnlRead(BaseModel):
    realized: float
    unrealized: float
    base_currency: str
    quote_currency: str


class AccountTradesSyncStateRead(BaseModel):
    last_trade_id: str | None = None
    last_trade_ts: datetime | None = None


class AccountAutoTradeEventRead(BaseModel):
    id: int
    event_type: str
    level: str
    message: str | None = None
    created_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class AccountTradesRead(BaseModel):
    account_id: int = Field(ge=1)
    symbol: str
    trades: list[AccountTradeRead] = Field(default_factory=list)
    pnl: AccountTradesPnlRead
    sync_state: AccountTradesSyncStateRead
    auto_trade_events: list[AccountAutoTradeEventRead] = Field(default_factory=list)
    sync_warnings: list[str] = Field(default_factory=list)
