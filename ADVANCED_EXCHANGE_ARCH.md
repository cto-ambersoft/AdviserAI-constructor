# Advanced Exchange Interaction Architecture

## Trade Service — Position Management, Dynamic SL/TP, Multi-TP, Indicator Watchers

---

## 1. Обзор проблемы и контекст

### 1.1 Текущее состояние (AS-IS)

Trade-сервис (FastAPI + Taskiq + PostgreSQL) уже имеет базовую auto-trade логику:

- Открытие позиций через ccxt
- SL/TP размещаются как **отдельные conditional orders** на бирже
- `auto_trade_positions` — плоская таблица с `entry_price`, `sl_price`, `tp_price`
- `exchange_trade_ledger` — синхронизация fills с биржи (фоновый Taskiq-таск каждую минуту)
- `exchange_order_metadata` — provenance map с `client_order_id`

### 1.2 Критические проблемы текущей архитектуры

**Binance Algo Migration (2025-12-09):** Conditional orders (STOP_MARKET, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET) мигрировали на Algo Service. Новые эндпоинты (`POST /fapi/v1/algoOrder`), новый WS event (`ALGO_UPDATE`). **Модификация незатриггеренных conditional orders НЕ поддерживается** — только cancel + re-place. Старый endpoint возвращает `-4120 STOP_ORDER_SWITCH_ALGO`.

**Bybit native partial TP/SL:** Bybit V5 поддерживает `tpslMode: "Partial"` через `/v5/position/trading-stop` — можно ставить множественные TP/SL на одну позицию нативно. Amend order (`/v5/order/amend`) работает для модификации "на месте".

**ccxt coverage gap:** ccxt может не полностью абстрагировать Binance Algo Orders API. Нужен adapter layer поверх ccxt.

### 1.3 Целевое состояние (TO-BE)

- Position State Machine с явными состояниями и переходами
- Dynamic SL/TP: trailing stop, breakeven, volatility-based
- Multi-TP: частичные закрытия с пересчётом exposure
- In-position indicator monitoring (RSI, MACD, MA crossover, ATR)
- Configurable priority chain для SL/TP adjustments
- Fault tolerance: reconnection, state sync, rate-limit aware queue
- Exchange-agnostic adapter layer (Binance + Bybit, extensible)

---

## 2. Архитектура верхнего уровня

```
┌─────────────────────────────────────────────────────────────┐
│                     Trade Service (FastAPI)                   │
│                                                               │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────────┐  │
│  │ REST API  │  │  Auto-Trade   │  │  Position State Machine │  │
│  │ endpoints │──│  Orchestrator │──│  (per-position FSM)     │  │
│  └──────────┘  └──────┬───────┘  └────────────┬───────────┘  │
│                       │                        │               │
│         ┌─────────────┼────────────────────────┤               │
│         │             │                        │               │
│  ┌──────▼──────┐ ┌────▼──────────┐ ┌──────────▼───────────┐  │
│  │  Watcher     │ │  SL/TP        │ │  Order Execution     │  │
│  │  Engine      │ │  Adjustment   │ │  Queue               │  │
│  │  (Taskiq)    │ │  Pipeline     │ │  (Priority-based)    │  │
│  └──────┬───────┘ └───────┬──────┘ └──────────┬───────────┘  │
│         │                 │                    │               │
│         └─────────────────┼────────────────────┘               │
│                           │                                    │
│                  ┌────────▼────────┐                           │
│                  │  Exchange        │                           │
│                  │  Adapter Layer   │                           │
│                  └────────┬────────┘                           │
│                           │                                    │
│              ┌────────────┼────────────┐                      │
│              │                         │                      │
│     ┌────────▼────────┐     ┌─────────▼─────────┐            │
│     │ Binance Adapter  │     │  Bybit Adapter     │            │
│     │ (Algo Orders +   │     │  (trading-stop +   │            │
│     │  User Data WS)   │     │   amend + WS)      │            │
│     └──────────────────┘     └───────────────────┘            │
│                                                               │
│  ┌──────────────────┐  ┌──────────────────────────────────┐  │
│  │  WebSocket Manager│  │  Exchange Trade Sync (existing)  │  │
│  │  (per-account,    │  │  + enhanced reconciliation       │  │
│  │   auto-reconnect) │  │                                  │  │
│  └──────────────────┘  └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Exchange Adapter Layer

### 3.1 Зачем нужен adapter поверх ccxt

ccxt — отличная base library, но для production auto-trade нужен дополнительный слой:

1. **Binance Algo Orders** — ccxt может не поддерживать `/fapi/v1/algoOrder` и `ALGO_UPDATE` event. Нужен fallback на raw HTTP.
2. **Bybit native partial TP/SL** — ccxt не абстрагирует `tpslMode: "Partial"` и `/v5/position/trading-stop`.
3. **Cancel-and-replace atomicity** — нужна retry-логика специфичная для каждой биржи.
4. **Rate limit tracking** — нужно парсить headers (`X-MBX-ORDER-COUNT`, `X-RateLimit-*`) и адаптивно throttle'ить.

### 3.2 Интерфейс ExchangeAdapter (ABC)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"

class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"  # One-way mode

@dataclass
class ConditionalOrderResult:
    exchange_order_id: str        # Binance: algoId, Bybit: orderId
    client_order_id: str
    order_type: str               # "stop_loss" | "take_profit" | "trailing_stop"
    trigger_price: float
    quantity: float
    status: str                   # "new" | "triggered" | "cancelled" | "rejected"
    is_algo: bool = False         # True for Binance algo orders

@dataclass
class PartialCloseResult:
    executed_qty: float
    avg_price: float
    remaining_qty: float
    order_id: str
    commission: float

@dataclass
class PositionSnapshot:
    """Exchange-side position state for reconciliation."""
    symbol: str
    side: PositionSide
    size: float                   # Absolute qty
    entry_price: float
    unrealized_pnl: float
    leverage: int
    mark_price: float
    liquidation_price: float
    open_orders: list[ConditionalOrderResult]  # Active SL/TP on exchange

@dataclass
class RateLimitState:
    order_count_10s: int
    order_count_1m: int
    order_limit_10s: int          # Binance: varies by VIP level
    order_limit_1m: int
    weight_used_1m: int
    weight_limit_1m: int
    retry_after: Optional[float]  # seconds until rate limit resets


class ExchangeAdapter(ABC):
    """
    Unified interface for exchange interactions.
    Each method handles exchange-specific quirks internally.
    """

    # ── Position Queries ──────────────────────────────────────

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[PositionSnapshot]:
        """Fetch current position state from exchange."""
        ...

    @abstractmethod
    async def get_open_conditional_orders(
        self, symbol: str
    ) -> list[ConditionalOrderResult]:
        """Fetch all active SL/TP/trailing orders for symbol."""
        ...

    # ── Order Placement ───────────────────────────────────────

    @abstractmethod
    async def place_stop_loss(
        self,
        symbol: str,
        side: OrderSide,          # SELL for long SL, BUY for short SL
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
        ...

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
        ...

    @abstractmethod
    async def place_trailing_stop(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        callback_rate: float,     # % distance from peak
        activation_price: Optional[float],
        client_order_id: str,
    ) -> ConditionalOrderResult:
        """
        Binance: algoOrder with TRAILING_STOP_MARKET
        Bybit:   /v5/position/trading-stop with trailingStop param
        """
        ...

    # ── Order Modification ────────────────────────────────────

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

        Binance: DELETE /fapi/v1/algoOrder → POST /fapi/v1/algoOrder
                 (no native amend for algo orders)
        Bybit:   POST /v5/position/trading-stop (modifies in-place)
                 OR /v5/order/amend for conditional orders

        CRITICAL: If cancel succeeds but new placement fails,
        must retry placement with exponential backoff.
        Position is UNPROTECTED until new SL is placed.
        """
        ...

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
        ...

    @abstractmethod
    async def cancel_conditional_order(
        self,
        symbol: str,
        order_id: str,
    ) -> bool:
        ...

    # ── Partial Close ─────────────────────────────────────────

    @abstractmethod
    async def partial_close(
        self,
        symbol: str,
        side: OrderSide,         # Opposite to position side
        quantity: float,
        client_order_id: str,
        order_type: str = "market",  # "market" | "limit"
        price: Optional[float] = None,
    ) -> PartialCloseResult:
        """
        Reduce position by given quantity.
        Uses reduce_only=True to prevent flipping.
        """
        ...

    # ── WebSocket / Streaming ─────────────────────────────────

    @abstractmethod
    async def subscribe_user_data(
        self,
        on_order_update: callable,     # ORDER_TRADE_UPDATE / ALGO_UPDATE
        on_position_update: callable,  # ACCOUNT_UPDATE
        on_disconnect: callable,
    ) -> None:
        """
        Start WebSocket user data stream.
        Binance: listenKey → wss://fstream.binance.com/ws/<listenKey>
                 Events: ORDER_TRADE_UPDATE, ALGO_UPDATE, ACCOUNT_UPDATE
        Bybit:   wss://stream.bybit.com/v5/private
                 Topics: order, execution, position
        """
        ...

    @abstractmethod
    async def subscribe_kline(
        self,
        symbol: str,
        interval: str,            # "1m", "5m", "15m", "1h"
        on_kline: callable,
    ) -> None:
        """Subscribe to kline stream for indicator calculations."""
        ...

    # ── Rate Limiting ─────────────────────────────────────────

    @abstractmethod
    def get_rate_limit_state(self) -> RateLimitState:
        """Return current rate limit consumption."""
        ...

    @abstractmethod
    def can_place_order(self) -> bool:
        """Check if we have rate limit headroom for an order."""
        ...
```

### 3.3 Binance Adapter — специфика реализации

```python
class BinanceAdapter(ExchangeAdapter):
    """
    Binance USDT-M Futures adapter.

    Key specifics:
    1. Algo Orders API for all conditional orders (since 2025-12-09)
    2. No amend for algo orders → cancel + re-place
    3. User Data Stream: listenKey + keepalive every 30 min
    4. Two event types: ORDER_TRADE_UPDATE (regular) + ALGO_UPDATE (conditional)
    5. Rate limits: IP-based weight + account-based order count
    """

    def __init__(self, ccxt_exchange, api_key: str, api_secret: str):
        self._ccxt = ccxt_exchange          # ccxt.binance instance
        self._api_key = api_key
        self._api_secret = api_secret
        self._rate_state = RateLimitState(...)
        self._listen_key: Optional[str] = None

    # ── Algo Order Placement (raw HTTP, NOT ccxt) ─────────────

    async def place_stop_loss(self, symbol, side, quantity,
                               trigger_price, client_order_id,
                               reduce_only=True):
        """
        POST /fapi/v1/algoOrder
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "type": "STOP_MARKET",
            "quantity": "0.001",
            "stopPrice": "95000",
            "timeInForce": "GTE_GTC",
            "workingType": "CONTRACT_PRICE",
            "priceProtect": true,
            "newClientAlgoId": "<client_order_id>",
            "reduceOnly": true
        }

        Response contains algoId — store this, NOT orderId.
        algoId is needed for cancellation via DELETE /fapi/v1/algoOrder.
        """
        ...

    async def cancel_and_replace_sl(self, symbol, existing_order_id,
                                     new_trigger_price, new_quantity,
                                     client_order_id):
        """
        CRITICAL SEQUENCE (no atomic amend for Binance algo orders):

        1. Cancel existing: DELETE /fapi/v1/algoOrder {algoId}
        2. Verify cancellation via ALGO_UPDATE WS event or GET query
        3. Place new SL: POST /fapi/v1/algoOrder
        4. If step 3 fails → EMERGENCY RETRY (position is unprotected!)

        Between step 1 and 3 there is a window where position
        has NO stop loss. This window must be minimized:
        - Pre-validate new price before cancelling
        - Use fire-and-forget cancel, immediately place new
        - If new placement fails: retry 3 times with 500ms delay
        - If still fails: place MARKET close as emergency fallback
          AND alert the user

        Rate limit awareness:
        - Cancel = 1 order count
        - New placement = 1 order count
        - Total = 2 counts per SL shift
        """
        ...

    # ── WebSocket: dual event handling ────────────────────────

    async def subscribe_user_data(self, on_order_update,
                                   on_position_update, on_disconnect):
        """
        1. POST /fapi/v1/listenKey → get listenKey
        2. Connect wss://fstream.binance.com/ws/<listenKey>
        3. Schedule keepalive PUT every 30 min
        4. Route events:
           - "e": "ORDER_TRADE_UPDATE"  → regular order fills
           - "e": "ALGO_UPDATE"         → conditional order state changes
           - "e": "ACCOUNT_UPDATE"      → position/balance changes

        ALGO_UPDATE statuses to handle:
        - NEW: algo order accepted
        - CANCELED: we cancelled or system cancelled
        - TRIGGERING: price hit, forwarding to matching engine
        - TRIGGERED: placed in matching engine
        - FINISHED: fill complete or cancelled in matching engine
        - REJECTED: margin check failed after trigger
        - EXPIRED: system cancelled (e.g., position closed)

        RECONNECTION:
        - On disconnect → wait 1s → new listenKey → reconnect
        - On reconnect → call get_position() to sync state
        - listenKey expires after 60 min without keepalive
        """
        ...
```

### 3.4 Bybit Adapter — специфика реализации

```python
class BybitAdapter(ExchangeAdapter):
    """
    Bybit V5 Unified Trading adapter.

    Key specifics:
    1. Native partial TP/SL via /v5/position/trading-stop (tpslMode=Partial)
    2. Order amend supported: /v5/order/amend (modify in-place, no cancel needed)
    3. WebSocket: private channel with auth handshake
    4. Rate limits: per-endpoint, 600 req / 5 sec per IP (default)
    5. Batch orders: /v5/order/create-batch (1-10 orders, separate rate pool)
    """

    def __init__(self, ccxt_exchange, api_key: str, api_secret: str):
        self._ccxt = ccxt_exchange          # ccxt.bybit instance
        self._api_key = api_key
        self._api_secret = api_secret

    # ── Native Partial TP/SL ──────────────────────────────────

    async def place_take_profit(self, symbol, side, quantity,
                                 trigger_price, client_order_id,
                                 reduce_only=True, limit_price=None):
        """
        POST /v5/position/trading-stop
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "takeProfit": "105000",
            "tpTriggerBy": "MarkPrice",
            "tpslMode": "Partial",
            "tpSize": "0.001",
            "tpOrderType": "Limit",      // or "Market"
            "tpLimitPrice": "104900",     // only if Limit
            "positionIdx": 0
        }

        ADVANTAGE over Binance:
        - Multiple partial TP orders can coexist on same position
        - System auto-adjusts qty if position shrinks
        - No need to manage separate order IDs for multi-TP
        """
        ...

    async def cancel_and_replace_sl(self, symbol, existing_order_id,
                                     new_trigger_price, new_quantity,
                                     client_order_id):
        """
        Bybit supports TWO approaches:

        Option A (preferred): /v5/position/trading-stop
        - Simply call again with new stopLoss value
        - For "Full position" mode: overwrites existing SL
        - ATOMIC — no unprotected window

        Option B: /v5/order/amend for conditional orders
        - Modify triggerPrice in-place
        - Also atomic

        Fallback to cancel + re-place only if amend returns error.
        """
        ...

    # ── WebSocket ─────────────────────────────────────────────

    async def subscribe_user_data(self, on_order_update,
                                   on_position_update, on_disconnect):
        """
        1. Connect wss://stream.bybit.com/v5/private
        2. Auth handshake: {"op": "auth", ...}
        3. Subscribe: {"op": "subscribe", "args": ["order", "execution", "position"]}

        Events:
        - topic "order"     → order status changes (including TP/SL triggers)
        - topic "execution" → fills
        - topic "position"  → position size/margin changes

        RECONNECTION:
        - Bybit WS ping every 20s, pong required
        - On disconnect → exponential backoff reconnect
        - On reconnect → full position sync via REST
        """
        ...
```

### 3.5 Adapter Factory

```python
class ExchangeAdapterFactory:
    """Creates appropriate adapter based on exchange name."""

    @staticmethod
    async def create(
        exchange_name: str,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
    ) -> ExchangeAdapter:
        if exchange_name == "binance":
            ccxt_ex = ccxt.binance({
                "apiKey": api_key,
                "secret": api_secret,
                "options": {"defaultType": "future"},
                "sandbox": testnet,
            })
            return BinanceAdapter(ccxt_ex, api_key, api_secret)

        elif exchange_name == "bybit":
            ccxt_ex = ccxt.bybit({
                "apiKey": api_key,
                "secret": api_secret,
                "options": {"defaultType": "swap"},
                "sandbox": testnet,
            })
            return BybitAdapter(ccxt_ex, api_key, api_secret)

        raise ValueError(f"Unsupported exchange: {exchange_name}")
```

---

## 4. Position State Machine

### 4.1 Состояния и переходы

```
                    ┌──────────────────────────────────────────┐
                    │                                          │
    ┌──────────┐    │  ┌──────────┐    ┌────────────┐         │
    │          │    │  │          │    │            │         │
───►│ PENDING  ├────┴─►│ ENTERING ├───►│   OPEN     │◄────────┘
    │          │       │          │    │            │
    └────┬─────┘       └────┬─────┘    └──┬───┬──┬─┘
         │                  │             │   │  │
         │ cancel           │ fill        │   │  │ indicator_trigger
         │ timeout          │ reject      │   │  │ trailing_tick
         │                  │ timeout     │   │  │ breakeven_reached
         ▼                  ▼             │   │  │ volatility_shift
    ┌──────────┐    ┌──────────┐         │   │  │
    │ CANCELLED│    │  FAILED  │         │   │  ▼
    └──────────┘    └──────────┘         │   │ ┌────────────┐
                                         │   │ │ ADJUSTING  │
                                         │   │ │ (SL/TP     │
                                         │   │ │  shifting) │
                                         │   │ └──────┬─────┘
                                         │   │        │
                                         │   │        │ adjustment_complete
                                         │   │        │ adjustment_failed
                                         │   │        │
                                         │   │  ┌─────▼──────┐
                                         │   └─►│  CLOSING   │
                                         │      │  (partial  │
                                         │      │   or full) │
                                         │      └──────┬─────┘
                                         │             │
                                         │             │ all_closed
                                         │             │ close_failed
                                         │             ▼
                                         │      ┌────────────┐
                                         └─────►│  CLOSED    │
                                                └────────────┘

    ┌──────────────┐
    │ RECONNECTING │ ← from any active state on WS disconnect
    │              │ → back to previous state on reconnect + sync
    └──────────────┘

    ┌────────────────┐
    │ ERROR_RECOVERY │ ← SL replacement failed, position unprotected
    │                │ → OPEN (after emergency SL placed)
    │                │ → CLOSED (after emergency market close)
    └────────────────┘
```

### 4.2 State Machine Implementation

```python
from enum import Enum
from typing import Optional, Callable
from datetime import datetime, timezone
import json


class PositionState(str, Enum):
    PENDING = "pending"
    ENTERING = "entering"
    OPEN = "open"
    ADJUSTING = "adjusting"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RECONNECTING = "reconnecting"
    ERROR_RECOVERY = "error_recovery"


class TransitionTrigger(str, Enum):
    # Entry
    ENTRY_SUBMITTED = "entry_submitted"
    ENTRY_FILLED = "entry_filled"
    ENTRY_REJECTED = "entry_rejected"
    ENTRY_TIMEOUT = "entry_timeout"
    ENTRY_CANCELLED = "entry_cancelled"

    # SL/TP adjustments
    INDICATOR_TRIGGER = "indicator_trigger"
    TRAILING_TICK = "trailing_tick"
    BREAKEVEN_REACHED = "breakeven_reached"
    VOLATILITY_SHIFT = "volatility_shift"
    ADJUSTMENT_COMPLETE = "adjustment_complete"
    ADJUSTMENT_FAILED = "adjustment_failed"

    # Closing
    SL_TRIGGERED = "sl_triggered"
    TP_TRIGGERED = "tp_triggered"
    PARTIAL_CLOSE = "partial_close"
    MANUAL_CLOSE = "manual_close"
    ALL_CLOSED = "all_closed"
    CLOSE_FAILED = "close_failed"

    # Fault tolerance
    WS_DISCONNECTED = "ws_disconnected"
    WS_RECONNECTED = "ws_reconnected"
    SYNC_COMPLETE = "sync_complete"
    EMERGENCY_SL_PLACED = "emergency_sl_placed"
    EMERGENCY_CLOSE = "emergency_close"


# ── Transition Table ──────────────────────────────────────────

VALID_TRANSITIONS: dict[PositionState, dict[TransitionTrigger, PositionState]] = {
    PositionState.PENDING: {
        TransitionTrigger.ENTRY_SUBMITTED: PositionState.ENTERING,
        TransitionTrigger.ENTRY_CANCELLED: PositionState.CANCELLED,
    },
    PositionState.ENTERING: {
        TransitionTrigger.ENTRY_FILLED: PositionState.OPEN,
        TransitionTrigger.ENTRY_REJECTED: PositionState.FAILED,
        TransitionTrigger.ENTRY_TIMEOUT: PositionState.FAILED,
        TransitionTrigger.ENTRY_CANCELLED: PositionState.CANCELLED,
    },
    PositionState.OPEN: {
        # SL/TP adjustment triggers → ADJUSTING
        TransitionTrigger.INDICATOR_TRIGGER: PositionState.ADJUSTING,
        TransitionTrigger.TRAILING_TICK: PositionState.ADJUSTING,
        TransitionTrigger.BREAKEVEN_REACHED: PositionState.ADJUSTING,
        TransitionTrigger.VOLATILITY_SHIFT: PositionState.ADJUSTING,

        # Close triggers
        TransitionTrigger.SL_TRIGGERED: PositionState.CLOSING,
        TransitionTrigger.TP_TRIGGERED: PositionState.CLOSING,
        TransitionTrigger.PARTIAL_CLOSE: PositionState.CLOSING,
        TransitionTrigger.MANUAL_CLOSE: PositionState.CLOSING,

        # Fault tolerance
        TransitionTrigger.WS_DISCONNECTED: PositionState.RECONNECTING,
    },
    PositionState.ADJUSTING: {
        TransitionTrigger.ADJUSTMENT_COMPLETE: PositionState.OPEN,
        TransitionTrigger.ADJUSTMENT_FAILED: PositionState.ERROR_RECOVERY,

        # Can still be closed while adjusting
        TransitionTrigger.SL_TRIGGERED: PositionState.CLOSING,
        TransitionTrigger.TP_TRIGGERED: PositionState.CLOSING,
        TransitionTrigger.MANUAL_CLOSE: PositionState.CLOSING,

        TransitionTrigger.WS_DISCONNECTED: PositionState.RECONNECTING,
    },
    PositionState.CLOSING: {
        TransitionTrigger.ALL_CLOSED: PositionState.CLOSED,
        TransitionTrigger.CLOSE_FAILED: PositionState.ERROR_RECOVERY,
        # Partial close returns to OPEN if position still has remaining size
        TransitionTrigger.PARTIAL_CLOSE: PositionState.OPEN,
        TransitionTrigger.WS_DISCONNECTED: PositionState.RECONNECTING,
    },
    PositionState.RECONNECTING: {
        TransitionTrigger.WS_RECONNECTED: PositionState.OPEN,  # temporary
        TransitionTrigger.SYNC_COMPLETE: PositionState.OPEN,
        TransitionTrigger.ALL_CLOSED: PositionState.CLOSED,    # closed while offline
    },
    PositionState.ERROR_RECOVERY: {
        TransitionTrigger.EMERGENCY_SL_PLACED: PositionState.OPEN,
        TransitionTrigger.EMERGENCY_CLOSE: PositionState.CLOSED,
    },
}


class PositionStateMachine:
    """
    Per-position FSM with guard conditions and transition logging.
    """

    def __init__(self, position_id: str, initial_state: PositionState = PositionState.PENDING):
        self.position_id = position_id
        self.state = initial_state
        self._pre_reconnect_state: Optional[PositionState] = None
        self._transition_log: list[dict] = []

    def can_transition(self, trigger: TransitionTrigger) -> bool:
        """Check if transition is valid without executing it."""
        transitions = VALID_TRANSITIONS.get(self.state, {})
        return trigger in transitions

    def transition(
        self,
        trigger: TransitionTrigger,
        reason: str = "",
        metadata: Optional[dict] = None,
    ) -> PositionState:
        """
        Execute state transition with guard checks.
        Raises InvalidTransitionError if not allowed.
        """
        transitions = VALID_TRANSITIONS.get(self.state, {})
        if trigger not in transitions:
            raise InvalidTransitionError(
                f"Cannot {trigger.value} in state {self.state.value} "
                f"for position {self.position_id}"
            )

        old_state = self.state
        new_state = transitions[trigger]

        # Guard: save pre-reconnect state
        if new_state == PositionState.RECONNECTING:
            self._pre_reconnect_state = old_state

        # Guard: restore pre-reconnect state on sync
        if trigger == TransitionTrigger.SYNC_COMPLETE and self._pre_reconnect_state:
            new_state = self._pre_reconnect_state
            self._pre_reconnect_state = None

        self.state = new_state

        # Log transition
        self._transition_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "from": old_state.value,
            "to": new_state.value,
            "trigger": trigger.value,
            "reason": reason,
            "metadata": metadata or {},
        })

        return new_state

    def get_transition_log(self) -> list[dict]:
        return self._transition_log.copy()


class InvalidTransitionError(Exception):
    pass
```

### 4.3 PositionContext — полная модель позиции

```python
@dataclass
class SLHistoryEntry:
    timestamp: str              # ISO 8601
    old_price: float
    new_price: float
    reason: str                 # "trailing" | "breakeven" | "volatility" | "indicator:RSI>75" | "manual"
    trigger_source: str         # "watcher" | "trailing_engine" | "breakeven_engine" | "user"
    exchange_order_id: str      # New order ID on exchange

@dataclass
class TPHistoryEntry:
    timestamp: str
    tp_level: int               # 0-based index
    old_price: float
    new_price: float
    reason: str
    close_pct: float            # % of original position closed at this TP
    exchange_order_id: str

@dataclass
class TPLevel:
    level: int                  # 0, 1, 2, ...
    price_offset_pct: float     # % offset from entry
    close_pct: float            # % of ORIGINAL position to close
    trigger_price: float        # Computed absolute price
    status: str                 # "pending" | "active" | "triggered" | "cancelled"
    exchange_order_id: Optional[str]

@dataclass
class WatcherConfig:
    indicator: str              # "RSI" | "MACD" | "MA_CROSSOVER" | "ATR"
    params: dict                # {"period": 14, "timeframe": "15m"}
    condition: str              # "RSI > 75" | "MA_CROSS_BEARISH" | "ATR_EXPAND > 1.5"
    action: str                 # "tighten_sl" | "move_tp" | "close_partial" | "alert"
    action_params: dict         # {"sl_offset_atr": 1.5} | {"close_pct": 25}
    is_active: bool

@dataclass
class PositionContext:
    """
    Complete position state — persisted in DB,
    updated on every state change.
    """
    # Identity
    position_id: str            # UUID
    user_id: str
    account_id: str             # Exchange account reference
    exchange: str               # "binance" | "bybit"
    symbol: str                 # "BTCUSDT"

    # State machine
    state: PositionState
    state_machine: PositionStateMachine  # Transient, rebuilt from DB

    # Entry
    side: PositionSide          # LONG | SHORT
    entry_price: float
    original_quantity: float
    current_quantity: float     # Decreases on partial closes
    leverage: int

    # Stop Loss
    current_sl_price: float
    sl_exchange_order_id: Optional[str]
    sl_type: str                # "fixed" | "trailing" | "volatility" | "breakeven"
    sl_history: list[SLHistoryEntry]

    # Take Profit
    tp_mode: str                # "single" | "multi"
    tp_levels: list[TPLevel]    # For multi-TP
    current_tp_price: Optional[float]  # For single TP
    tp_history: list[TPHistoryEntry]

    # Trailing stop config
    trailing_enabled: bool
    trailing_callback_rate: Optional[float]
    trailing_activation_price: Optional[float]
    trailing_highest_price: Optional[float]   # For long
    trailing_lowest_price: Optional[float]    # For short

    # Breakeven config
    breakeven_enabled: bool
    breakeven_trigger_rr: float  # e.g., 1.0 means R:R=1:1
    breakeven_activated: bool    # Already moved to breakeven?

    # Volatility SL config
    volatility_sl_enabled: bool
    volatility_atr_period: int
    volatility_atr_multiplier: float
    volatility_last_atr: Optional[float]

    # Watchers
    active_watchers: list[WatcherConfig]

    # Adjustment priority chain (configurable per strategy)
    adjustment_priority: list[str]  # ["watcher", "trailing", "breakeven", "volatility"]

    # Timing
    opened_at: str
    closed_at: Optional[str]
    last_adjusted_at: Optional[str]

    # PnL
    realized_pnl: float
    commission_total: float
```

### 4.4 Database Schema (Alembic migration)

```python
"""
Alembic migration: refactor auto_trade_positions for Position State Machine.
"""

def upgrade():
    # Add new columns to existing auto_trade_positions table
    op.add_column('auto_trade_positions', sa.Column(
        'state', sa.String(30), nullable=False, server_default='open'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'original_quantity', sa.Numeric(20, 8), nullable=True
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'current_quantity', sa.Numeric(20, 8), nullable=True
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'sl_type', sa.String(20), server_default='fixed'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'sl_exchange_order_id', sa.String(100), nullable=True
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'sl_history_json', sa.JSON, server_default='[]'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'tp_mode', sa.String(10), server_default='single'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'tp_levels_json', sa.JSON, server_default='[]'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'tp_history_json', sa.JSON, server_default='[]'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'trailing_config_json', sa.JSON, nullable=True
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'breakeven_config_json', sa.JSON, nullable=True
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'volatility_config_json', sa.JSON, nullable=True
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'active_watchers_json', sa.JSON, server_default='[]'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'adjustment_priority_json', sa.JSON,
        server_default='["watcher","trailing","breakeven","volatility"]'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'transition_log_json', sa.JSON, server_default='[]'
    ))
    op.add_column('auto_trade_positions', sa.Column(
        'last_adjusted_at', sa.DateTime(timezone=True), nullable=True
    ))

    # Index for active position lookups
    op.create_index(
        'ix_positions_user_state',
        'auto_trade_positions',
        ['user_id', 'state'],
        postgresql_where=sa.text("state NOT IN ('closed', 'cancelled', 'failed')")
    )

def downgrade():
    op.drop_index('ix_positions_user_state')
    # Drop all added columns...
```

---

## 5. Order Execution Queue (Priority-Based)

### 5.1 Приоритеты

Когда несколько adjustments происходят одновременно (watcher fired + trailing tick + volatility recalc), нужна очередь с приоритетами. **SL-ордера всегда имеют наивысший приоритет** — незащищённая позиция = катастрофа.

```python
from enum import IntEnum
import asyncio
from dataclasses import dataclass, field

class OrderPriority(IntEnum):
    """Lower number = higher priority."""
    EMERGENCY_SL = 0          # SL replacement after failed cancel-and-replace
    EMERGENCY_CLOSE = 1       # Emergency market close
    SL_ADJUSTMENT = 10        # Trailing/breakeven/volatility SL shift
    TP_ADJUSTMENT = 20        # TP level placement or shift
    PARTIAL_CLOSE = 30        # Multi-TP partial close execution
    NEW_CONDITIONAL = 40      # New SL/TP after position open
    CANCEL_ORDER = 50         # Cancel stale orders

@dataclass(order=True)
class OrderTask:
    priority: int
    created_at: float = field(compare=True)   # timestamp for FIFO within same priority
    position_id: str = field(compare=False)
    action: str = field(compare=False)         # "place_sl" | "replace_sl" | "place_tp" | ...
    params: dict = field(compare=False, default_factory=dict)
    retry_count: int = field(compare=False, default=0)
    max_retries: int = field(compare=False, default=3)


class OrderExecutionQueue:
    """
    Per-account priority queue for exchange orders.

    Key behaviors:
    1. SL orders always execute before TP orders
    2. Rate limit aware — pauses when approaching limits
    3. Deduplication — if same position+action already queued, replace params
    4. Retry with exponential backoff on transient errors
    5. Emergency escalation — if SL placement fails 3x, escalate to market close
    """

    def __init__(self, adapter: ExchangeAdapter, account_id: str):
        self._adapter = adapter
        self._account_id = account_id
        self._queue: asyncio.PriorityQueue[OrderTask] = asyncio.PriorityQueue()
        self._processing = False
        self._pending_tasks: dict[str, OrderTask] = {}  # key: f"{position_id}:{action}"

    async def enqueue(self, task: OrderTask) -> None:
        """Add task to queue, deduplicating by position+action."""
        key = f"{task.position_id}:{task.action}"

        if key in self._pending_tasks:
            # Replace with newer params (latest SL price is more relevant)
            existing = self._pending_tasks[key]
            existing.params = task.params
            return

        self._pending_tasks[key] = task
        await self._queue.put(task)

    async def start_processing(self) -> None:
        """Main processing loop — runs as asyncio task."""
        self._processing = True
        while self._processing:
            task = await self._queue.get()
            key = f"{task.position_id}:{task.action}"

            try:
                # Rate limit check
                if not self._adapter.can_place_order():
                    rate_state = self._adapter.get_rate_limit_state()
                    wait_time = rate_state.retry_after or 1.0
                    await asyncio.sleep(wait_time)

                await self._execute_task(task)

            except TransientExchangeError as e:
                if task.retry_count < task.max_retries:
                    task.retry_count += 1
                    backoff = min(0.5 * (2 ** task.retry_count), 10.0)
                    await asyncio.sleep(backoff)
                    await self._queue.put(task)
                else:
                    await self._handle_max_retries(task, e)

            except Exception as e:
                await self._handle_fatal_error(task, e)

            finally:
                self._pending_tasks.pop(key, None)
                self._queue.task_done()

    async def _handle_max_retries(self, task: OrderTask, error: Exception):
        """
        CRITICAL: If SL placement fails after all retries,
        escalate to emergency market close.
        """
        if task.action in ("place_sl", "replace_sl"):
            emergency = OrderTask(
                priority=OrderPriority.EMERGENCY_CLOSE,
                created_at=time.time(),
                position_id=task.position_id,
                action="emergency_market_close",
                params={"reason": f"SL placement failed: {error}"},
            )
            await self._queue.put(emergency)
```

---

## 6. SL/TP Adjustment Pipeline

### 6.1 Приоритетная цепочка

Когда несколько механизмов хотят сдвинуть SL одновременно, нужна детерминистичная приоритизация:

```python
class SLAdjustmentPipeline:
    """
    Evaluates all SL adjustment sources and picks the winner.

    Priority chain (configurable per strategy profile):
    1. Watcher rules (indicator-based) — highest priority
    2. Trailing stop — follows price
    3. Breakeven — one-time shift
    4. Volatility-based — ATR adaptation

    RULE: SL can only move in the PROTECTIVE direction.
    - Long position: SL can only move UP (tighter)
    - Short position: SL can only move DOWN (tighter)
    Exception: volatility-based can WIDEN SL if ATR expands
    (configurable via allow_sl_widen flag)
    """

    def __init__(self, position: PositionContext):
        self.position = position

    async def evaluate(
        self,
        current_price: float,
        indicators: dict,      # {"RSI": 76.5, "ATR": 1250.0, ...}
        kline_data: list,      # Recent OHLCV for trailing calc
    ) -> Optional[SLAdjustmentResult]:
        """
        Run all enabled adjustment sources in priority order.
        First source that produces a valid new SL wins.
        """
        candidates: list[SLAdjustmentResult] = []

        for source in self.position.adjustment_priority:
            result = await self._evaluate_source(
                source, current_price, indicators, kline_data
            )
            if result and result.is_valid:
                candidates.append(result)

        if not candidates:
            return None

        # Pick the most protective SL among all candidates
        if self.position.side == PositionSide.LONG:
            # For long: higher SL = more protective
            return max(candidates, key=lambda r: r.new_sl_price)
        else:
            # For short: lower SL = more protective
            return min(candidates, key=lambda r: r.new_sl_price)

    async def _evaluate_source(self, source, current_price, indicators, kline_data):
        if source == "watcher":
            return await self._evaluate_watcher_rules(indicators)
        elif source == "trailing":
            return self._evaluate_trailing(current_price)
        elif source == "breakeven":
            return self._evaluate_breakeven(current_price)
        elif source == "volatility":
            return self._evaluate_volatility(indicators.get("ATR"))
        return None

    # ── Trailing Stop ─────────────────────────────────────────

    def _evaluate_trailing(self, current_price: float) -> Optional[SLAdjustmentResult]:
        """
        Classic trailing stop: track highest (long) / lowest (short) price.
        SL = peak - callback_rate * peak (for long).

        Only moves SL in protective direction.
        """
        if not self.position.trailing_enabled:
            return None

        rate = self.position.trailing_callback_rate
        p = self.position

        if p.side == PositionSide.LONG:
            new_high = max(p.trailing_highest_price or p.entry_price, current_price)
            new_sl = new_high * (1 - rate / 100)
            if new_sl > p.current_sl_price:
                return SLAdjustmentResult(
                    new_sl_price=new_sl,
                    reason="trailing",
                    detail=f"peak={new_high:.2f}, callback={rate}%",
                    is_valid=True,
                    update_tracking={"trailing_highest_price": new_high},
                )
        else:  # SHORT
            new_low = min(p.trailing_lowest_price or p.entry_price, current_price)
            new_sl = new_low * (1 + rate / 100)
            if new_sl < p.current_sl_price:
                return SLAdjustmentResult(
                    new_sl_price=new_sl,
                    reason="trailing",
                    detail=f"trough={new_low:.2f}, callback={rate}%",
                    is_valid=True,
                    update_tracking={"trailing_lowest_price": new_low},
                )

        return None

    # ── Breakeven ─────────────────────────────────────────────

    def _evaluate_breakeven(self, current_price: float) -> Optional[SLAdjustmentResult]:
        """
        Move SL to entry price when R:R reaches breakeven_trigger_rr.

        Example: entry=100000, SL=98000, trigger_rr=1.0
        Risk = 2000, required reward = 2000
        When price reaches 102000 → move SL to 100000

        One-time operation: once activated, doesn't fire again.
        """
        p = self.position
        if not p.breakeven_enabled or p.breakeven_activated:
            return None

        risk = abs(p.entry_price - p.current_sl_price)
        required_move = risk * p.breakeven_trigger_rr

        if p.side == PositionSide.LONG:
            if current_price >= p.entry_price + required_move:
                return SLAdjustmentResult(
                    new_sl_price=p.entry_price,
                    reason="breakeven",
                    detail=f"R:R={p.breakeven_trigger_rr}, price={current_price:.2f}",
                    is_valid=True,
                    update_tracking={"breakeven_activated": True},
                )
        else:
            if current_price <= p.entry_price - required_move:
                return SLAdjustmentResult(
                    new_sl_price=p.entry_price,
                    reason="breakeven",
                    detail=f"R:R={p.breakeven_trigger_rr}, price={current_price:.2f}",
                    is_valid=True,
                    update_tracking={"breakeven_activated": True},
                )

        return None

    # ── Volatility-Based SL ───────────────────────────────────

    def _evaluate_volatility(self, current_atr: Optional[float]) -> Optional[SLAdjustmentResult]:
        """
        SL = entry_price ∓ (ATR * multiplier)

        ATR expands → SL widens (gives room)
        ATR contracts → SL tightens (locks profit)

        Recalculated on every watcher tick.
        allow_sl_widen controls whether ATR expansion can push SL further.
        """
        p = self.position
        if not p.volatility_sl_enabled or current_atr is None:
            return None

        distance = current_atr * p.volatility_atr_multiplier

        if p.side == PositionSide.LONG:
            new_sl = p.entry_price - distance
            # Only tighten (move up) unless widening allowed
            if new_sl > p.current_sl_price:
                return SLAdjustmentResult(
                    new_sl_price=new_sl,
                    reason="volatility",
                    detail=f"ATR={current_atr:.2f}, mult={p.volatility_atr_multiplier}",
                    is_valid=True,
                    update_tracking={"volatility_last_atr": current_atr},
                )
        else:
            new_sl = p.entry_price + distance
            if new_sl < p.current_sl_price:
                return SLAdjustmentResult(
                    new_sl_price=new_sl,
                    reason="volatility",
                    detail=f"ATR={current_atr:.2f}, mult={p.volatility_atr_multiplier}",
                    is_valid=True,
                    update_tracking={"volatility_last_atr": current_atr},
                )

        return None
```

---

## 7. Multi-TP (Partial Close) Engine

### 7.1 Конфигурация

```python
# Example multi-TP configuration in strategy profile:
tp_config = {
    "mode": "multi",
    "levels": [
        {"price_offset_pct": 1.5, "close_pct": 33, "move_sl_to": "breakeven"},
        {"price_offset_pct": 3.0, "close_pct": 33, "move_sl_to": "tp1"},
        {"price_offset_pct": 5.0, "close_pct": 34, "move_sl_to": None},  # Full close
    ]
}

# For a LONG position at entry 100000:
# TP1: 101500 → close 33%, SL → 100000 (breakeven)
# TP2: 103000 → close 33%, SL → 101500 (previous TP1)
# TP3: 105000 → close remaining 34%
```

### 7.2 Multi-TP Execution Logic

```python
class MultiTPEngine:
    """
    Manages staged take-profit with partial closes.

    Exchange-specific behavior:

    BINANCE:
    - Place each TP as separate algo order (TAKE_PROFIT_MARKET)
    - On TP1 trigger → ALGO_UPDATE event → execute partial close
    - Cancel remaining TP orders, recalculate quantities, re-place
    - Cancel existing SL, place new SL at breakeven

    BYBIT:
    - Use tpslMode=Partial → place all TP levels at once
    - Bybit auto-adjusts qty if position shrinks
    - On TP1 trigger → position update event → adjust SL
    - Much simpler — fewer API calls needed
    """

    def __init__(self, position: PositionContext, adapter: ExchangeAdapter,
                 order_queue: OrderExecutionQueue):
        self.position = position
        self.adapter = adapter
        self.queue = order_queue

    async def initialize_tp_levels(self) -> None:
        """Place all TP orders on exchange after position opens."""
        for level in self.position.tp_levels:
            if level.status != "pending":
                continue

            task = OrderTask(
                priority=OrderPriority.NEW_CONDITIONAL,
                created_at=time.time(),
                position_id=self.position.position_id,
                action="place_tp",
                params={
                    "level": level.level,
                    "trigger_price": level.trigger_price,
                    "quantity": self.position.original_quantity * (level.close_pct / 100),
                },
            )
            await self.queue.enqueue(task)

    async def handle_tp_triggered(self, triggered_level: int) -> None:
        """
        Called when a TP level is hit (from WS event).

        Sequence:
        1. Update position.current_quantity
        2. Log TP history entry
        3. Handle SL adjustment per level config
        4. If not last TP → recalculate remaining TP quantities
        5. If last TP → transition to CLOSED
        """
        level = self.position.tp_levels[triggered_level]
        close_qty = self.position.original_quantity * (level.close_pct / 100)

        # Update position
        self.position.current_quantity -= close_qty
        self.position.realized_pnl += self._calc_pnl(level.trigger_price, close_qty)

        # Log
        self.position.tp_history.append(TPHistoryEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            tp_level=triggered_level,
            old_price=level.trigger_price,
            new_price=level.trigger_price,
            reason=f"tp_level_{triggered_level}_hit",
            close_pct=level.close_pct,
            exchange_order_id=level.exchange_order_id or "",
        ))

        level.status = "triggered"

        # SL adjustment based on level config
        move_sl_to = level.__dict__.get("move_sl_to")
        if move_sl_to == "breakeven":
            new_sl = self.position.entry_price
        elif move_sl_to and move_sl_to.startswith("tp"):
            prev_level_idx = int(move_sl_to.replace("tp", "")) - 1
            new_sl = self.position.tp_levels[prev_level_idx].trigger_price
        else:
            new_sl = None

        if new_sl and new_sl != self.position.current_sl_price:
            await self.queue.enqueue(OrderTask(
                priority=OrderPriority.SL_ADJUSTMENT,
                created_at=time.time(),
                position_id=self.position.position_id,
                action="replace_sl",
                params={
                    "new_trigger_price": new_sl,
                    "new_quantity": self.position.current_quantity,
                    "reason": f"tp{triggered_level+1}_hit_sl_adjustment",
                },
            ))

        # Check if fully closed
        remaining_levels = [l for l in self.position.tp_levels if l.status == "pending"]
        if not remaining_levels or self.position.current_quantity <= 0:
            self.position.state_machine.transition(
                TransitionTrigger.ALL_CLOSED,
                reason=f"All TP levels hit"
            )
        else:
            # Return to OPEN state for continued monitoring
            self.position.state_machine.transition(
                TransitionTrigger.PARTIAL_CLOSE,
                reason=f"TP{triggered_level+1} partial close, remaining={self.position.current_quantity}"
            )
```

---

## 8. In-Position Indicator Watcher

### 8.1 Architecture

```
 ┌──────────────────────────────────────┐
 │      Watcher Engine (Taskiq)          │
 │                                       │
 │  ┌────────────┐   ┌───────────────┐  │
 │  │ Kline WS   │──►│ Indicator     │  │
 │  │ Subscriber │   │ Calculator    │  │
 │  └────────────┘   │ (ta-lib/      │  │
 │                    │  pandas_ta)   │  │
 │                    └───────┬───────┘  │
 │                            │          │
 │                    ┌───────▼───────┐  │
 │                    │ Rule Engine   │  │
 │                    │ (condition    │  │
 │                    │  evaluator)   │  │
 │                    └───────┬───────┘  │
 │                            │          │
 │                    ┌───────▼───────┐  │
 │                    │ Event Bus     │──┼──► SL/TP Adjustment Pipeline
 │                    │ (Redis pub/   │  │    Order Execution Queue
 │                    │  sub)         │  │    Notification Service
 │                    └───────────────┘  │
 └──────────────────────────────────────┘
```

### 8.2 Watcher Implementation

```python
import pandas_ta as ta
import pandas as pd
from app.worker.broker import broker  # Taskiq broker


class IndicatorWatcher:
    """
    Computes indicators on live kline data and evaluates rules.
    Runs as Taskiq periodic task per active position.
    """

    SUPPORTED_INDICATORS = {
        "RSI": lambda df, params: ta.rsi(df["close"], length=params.get("period", 14)),
        "MACD": lambda df, params: ta.macd(
            df["close"],
            fast=params.get("fast", 12),
            slow=params.get("slow", 26),
            signal=params.get("signal", 9)
        ),
        "ATR": lambda df, params: ta.atr(
            df["high"], df["low"], df["close"],
            length=params.get("period", 14)
        ),
        "EMA": lambda df, params: ta.ema(df["close"], length=params.get("period", 21)),
        "SMA": lambda df, params: ta.sma(df["close"], length=params.get("period", 50)),
    }

    def __init__(self, position: PositionContext, adapter: ExchangeAdapter):
        self.position = position
        self.adapter = adapter
        self._kline_buffer: dict[str, pd.DataFrame] = {}  # keyed by timeframe

    async def tick(self) -> list[WatcherEvent]:
        """
        Called on every new kline close (or periodic interval).
        Returns list of triggered events.
        """
        events = []

        for watcher in self.position.active_watchers:
            if not watcher.is_active:
                continue

            # Get kline data for this indicator's timeframe
            timeframe = watcher.params.get("timeframe", "15m")
            df = self._kline_buffer.get(timeframe)
            if df is None or len(df) < 50:
                continue

            # Calculate indicator
            calc_fn = self.SUPPORTED_INDICATORS.get(watcher.indicator)
            if not calc_fn:
                continue

            indicator_values = calc_fn(df, watcher.params)

            # Get latest value
            if isinstance(indicator_values, pd.DataFrame):
                # MACD returns DataFrame with multiple columns
                latest = indicator_values.iloc[-1].to_dict()
            else:
                latest = float(indicator_values.iloc[-1])

            # Evaluate condition
            triggered = self._evaluate_condition(
                watcher.condition, latest, watcher.indicator
            )

            if triggered:
                events.append(WatcherEvent(
                    position_id=self.position.position_id,
                    indicator=watcher.indicator,
                    condition=watcher.condition,
                    current_value=latest,
                    action=watcher.action,
                    action_params=watcher.action_params,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ))

        return events

    def _evaluate_condition(self, condition: str, value, indicator: str) -> bool:
        """
        Evaluate conditions like:
        - "RSI > 75"
        - "RSI < 30"
        - "MACD_CROSS_BEARISH"
        - "ATR > 1500"
        - "EMA_21 < EMA_50" (MA crossover)
        """
        if ">" in condition and "_CROSS" not in condition:
            parts = condition.split(">")
            threshold = float(parts[1].strip())
            return float(value) > threshold

        elif "<" in condition and "_CROSS" not in condition:
            parts = condition.split("<")
            threshold = float(parts[1].strip())
            return float(value) < threshold

        elif "MACD_CROSS_BEARISH" in condition:
            if isinstance(value, dict):
                macd_line = value.get("MACD_12_26_9", 0)
                signal_line = value.get("MACDs_12_26_9", 0)
                return macd_line < signal_line
            return False

        elif "MACD_CROSS_BULLISH" in condition:
            if isinstance(value, dict):
                macd_line = value.get("MACD_12_26_9", 0)
                signal_line = value.get("MACDs_12_26_9", 0)
                return macd_line > signal_line
            return False

        return False


# ── Taskiq task for watcher scheduling ────────────────────────

@broker.task(task_name="position_watcher_tick")
async def position_watcher_tick(position_id: str) -> None:
    """
    Periodic task: compute indicators and evaluate rules for one position.
    Scheduled when position enters OPEN state.
    Cancelled when position leaves OPEN/ADJUSTING state.

    Frequency: every kline close for the fastest active timeframe
    (typically every 1m for 1m indicators, every 15m for 15m indicators).
    """
    # Load position context from DB
    position = await load_position_context(position_id)
    if position.state not in (PositionState.OPEN, PositionState.ADJUSTING):
        return  # Position no longer active, skip

    # Create adapter and watcher
    adapter = await ExchangeAdapterFactory.create(
        position.exchange, ...,
    )
    watcher = IndicatorWatcher(position, adapter)

    # Fetch latest klines for each needed timeframe
    for tf in _get_required_timeframes(position.active_watchers):
        klines = await adapter._ccxt.fetch_ohlcv(
            position.symbol, tf, limit=100
        )
        watcher._kline_buffer[tf] = pd.DataFrame(
            klines, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )

    # Run tick
    events = await watcher.tick()

    # Publish events to Redis for processing
    for event in events:
        await publish_watcher_event(event)


# ── Event handler ─────────────────────────────────────────────

async def handle_watcher_event(event: WatcherEvent) -> None:
    """
    Process indicator trigger event.
    Translates watcher action into FSM transition + order queue task.
    """
    position = await load_position_context(event.position_id)
    queue = get_order_queue(position.account_id)

    if event.action == "tighten_sl":
        # Calculate new SL based on action_params
        offset = event.action_params.get("sl_offset_atr", 1.5)
        current_atr = event.action_params.get("current_atr", 0)
        new_sl = compute_tightened_sl(position, offset, current_atr)

        position.state_machine.transition(
            TransitionTrigger.INDICATOR_TRIGGER,
            reason=f"{event.indicator}: {event.condition}"
        )

        await queue.enqueue(OrderTask(
            priority=OrderPriority.SL_ADJUSTMENT,
            created_at=time.time(),
            position_id=position.position_id,
            action="replace_sl",
            params={"new_trigger_price": new_sl, "reason": f"indicator:{event.condition}"},
        ))

    elif event.action == "close_partial":
        close_pct = event.action_params.get("close_pct", 25)
        close_qty = position.current_quantity * (close_pct / 100)

        await queue.enqueue(OrderTask(
            priority=OrderPriority.PARTIAL_CLOSE,
            created_at=time.time(),
            position_id=position.position_id,
            action="partial_close",
            params={"quantity": close_qty, "reason": f"indicator:{event.condition}"},
        ))

    elif event.action == "alert":
        await send_user_notification(position.user_id, event)
```

---

## 9. WebSocket Manager & Fault Tolerance

### 9.1 Data Quality Guards

Три простых проверки, встроенных в `WebSocketManager`, которые предотвращают
действия на основе грязных данных. Не требуют отдельных классов или инфраструктуры —
это `if`-проверки в правильных местах.

**Warmup gate.** Первые 3–5 секунд после любого (ре)коннекта данные ненадёжны —
биржа отдаёт кэшированные снапшоты, а не live-тики. В этот период WS-события
логируются и обновляют локальный кэш цен, но **не передаются** в watcher engine
и SL/TP pipeline. Предотвращает ложные срабатывания trailing stop или watcher rules
на stale данных сразу после reconnect.

**Stale tick guard.** Перед тем как price-событие дойдёт до adjustment pipeline,
проверяется дельта с последней известной ценой. Если скачок превышает порог
(настраиваемый, по умолчанию 2% для BTC, 5% для альткоинов) — тик отбрасывается
и логируется. Защищает от единичного аномального тика, который мог бы сдвинуть SL
или сработать как TP. Порог подбирается по волатильности инструмента, не hardcoded.

**Connection health (jitter EMA).** Отслеживается экспоненциальная скользящая средняя
интервалов между heartbeat/pong от биржи. Если jitter растёт выше порога — это ранний
сигнал деградации соединения. Вместо ожидания полного disconnect, менеджер
инициирует **proactive reconnect**: закрывает текущий WS и открывает новый,
с полным state sync. Это сокращает окно потерянных данных с десятков секунд
(пассивное ожидание timeout) до 3–5 секунд (активный reconnect + warmup).

### 9.2 Per-Account WebSocket Lifecycle

```python
class WebSocketManager:
    """
    Manages WebSocket connections per exchange account.

    Responsibilities:
    1. Maintain persistent connection to user data stream
    2. Auto-reconnect with exponential backoff
    3. Route events to position state machines
    4. Full state sync on reconnection
    5. Heartbeat monitoring
    6. Data quality guards (warmup, stale tick, jitter health)
    """

    # ── Config ────────────────────────────────────────────────
    WARMUP_SECONDS = 4.0              # Ignore ticks for decisions during this window
    STALE_TICK_THRESHOLD_PCT = 0.02   # 2% default; override per-symbol from exchangeInfo
    JITTER_EMA_ALPHA = 0.3            # Smoothing factor for jitter tracking
    JITTER_UNHEALTHY_MS = 5000        # If avg heartbeat gap > 5s, connection is degraded
    PROACTIVE_RECONNECT_COOLDOWN = 30 # Min seconds between proactive reconnects

    def __init__(self, adapter: ExchangeAdapter, account_id: str):
        self.adapter = adapter
        self.account_id = account_id
        self._connected = False
        self._positions: dict[str, PositionContext] = {}  # symbol → context
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 50
        self._base_backoff = 1.0  # seconds

        # Data quality state
        self._warmed_up = False
        self._warmup_started_at: float = 0
        self._last_prices: dict[str, float] = {}     # symbol → last known good price
        self._last_heartbeat_at: float = 0
        self._jitter_ema_ms: float = 0                # Smoothed heartbeat interval
        self._last_proactive_reconnect_at: float = 0

    async def start(self) -> None:
        """Start WS connection and event routing."""
        self._warmed_up = False
        self._warmup_started_at = time.time()

        await self.adapter.subscribe_user_data(
            on_order_update=self._handle_order_update,
            on_position_update=self._handle_position_update,
            on_disconnect=self._handle_disconnect,
        )
        self._connected = True
        self._reconnect_attempts = 0
        self._last_heartbeat_at = time.time()

        logger.info(f"[{self.account_id}] WS connected, warmup started ({self.WARMUP_SECONDS}s)")

    # ── Data Quality Guards ───────────────────────────────────

    def _check_warmup(self) -> bool:
        """
        Returns True if connection is warmed up and data is trustworthy.
        During warmup: events still update _last_prices cache,
        but are NOT forwarded to decision-making components.
        """
        if self._warmed_up:
            return True

        elapsed = time.time() - self._warmup_started_at
        if elapsed >= self.WARMUP_SECONDS:
            self._warmed_up = True
            logger.info(f"[{self.account_id}] WS warmup complete after {elapsed:.1f}s")
            return True

        return False

    def _check_stale_tick(self, symbol: str, price: float) -> bool:
        """
        Returns True if tick is valid (within acceptable delta).
        Returns False if tick is stale/anomalous — should be dropped.

        Always updates _last_prices regardless of result,
        so the cache stays current even if we reject a tick for decisions.
        """
        last = self._last_prices.get(symbol)
        self._last_prices[symbol] = price

        if last is None:
            return True  # First tick for this symbol, accept

        if last == 0:
            return True

        delta_pct = abs(price - last) / last
        threshold = self._get_stale_threshold(symbol)

        if delta_pct > threshold:
            logger.warning(
                f"[{self.account_id}] Stale tick rejected: {symbol} "
                f"price={price}, last={last}, delta={delta_pct:.4%} > {threshold:.2%}"
            )
            return False

        return True

    def _get_stale_threshold(self, symbol: str) -> float:
        """
        Per-symbol threshold. BTC/ETH = tighter (2%),
        altcoins = looser (5%). Can be loaded from config.
        """
        major = ("BTCUSDT", "ETHUSDT")
        if symbol in major:
            return 0.02
        return 0.05

    def update_heartbeat(self) -> None:
        """
        Called on every pong/heartbeat from exchange.
        Tracks jitter EMA and triggers proactive reconnect if degraded.
        """
        now = time.time()
        if self._last_heartbeat_at > 0:
            gap_ms = (now - self._last_heartbeat_at) * 1000
            self._jitter_ema_ms = (
                self.JITTER_EMA_ALPHA * gap_ms
                + (1 - self.JITTER_EMA_ALPHA) * self._jitter_ema_ms
            )

        self._last_heartbeat_at = now

    async def _check_connection_health(self) -> None:
        """
        If jitter EMA exceeds threshold, the connection is degrading.
        Proactively reconnect instead of waiting for full disconnect.

        Cooldown prevents reconnect storms.
        """
        if self._jitter_ema_ms <= self.JITTER_UNHEALTHY_MS:
            return

        now = time.time()
        if now - self._last_proactive_reconnect_at < self.PROACTIVE_RECONNECT_COOLDOWN:
            return

        logger.warning(
            f"[{self.account_id}] Connection degraded: "
            f"jitter_ema={self._jitter_ema_ms:.0f}ms > {self.JITTER_UNHEALTHY_MS}ms. "
            f"Proactive reconnect."
        )
        self._last_proactive_reconnect_at = now
        await self._handle_disconnect()

    # ── Event Routing (with guards applied) ───────────────────

    async def _handle_order_update(self, event: dict) -> None:
        """
        Route order/algo update events to position state machines.
        Guards applied in sequence: warmup → stale tick → forward.
        """
        symbol = event.get("symbol", "")
        price = float(event.get("price", 0) or event.get("trigger_price", 0) or 0)

        # Guard 1: Update price cache always, but check warmup for decisions
        if price > 0:
            tick_valid = self._check_stale_tick(symbol, price)
        else:
            tick_valid = True

        # Guard 2: During warmup, only log — don't act
        if not self._check_warmup():
            logger.debug(f"[{self.account_id}] Warmup: buffering {event.get('type', 'unknown')} for {symbol}")
            return

        # Guard 3: Stale tick — skip for SL/TP/watcher decisions
        if not tick_valid:
            return

        # Forward to position-specific handler
        position = self._positions.get(symbol)
        if position:
            await self._route_to_position(position, event)

    async def _handle_position_update(self, event: dict) -> None:
        """
        Handle position size/margin changes from exchange.
        These are always processed (even during warmup) since they
        represent confirmed state changes, not price ticks.
        """
        symbol = event.get("symbol", "")
        position = self._positions.get(symbol)
        if not position:
            return

        new_size = abs(float(event.get("size", 0)))

        if new_size == 0 and position.state != PositionState.CLOSED:
            # Position closed externally (user, liquidation, TP/SL)
            position.state_machine.transition(
                TransitionTrigger.ALL_CLOSED,
                reason=f"Position closed: {event.get('reason', 'unknown')}"
            )
            await persist_position(position)
        elif new_size != position.current_quantity:
            position.current_quantity = new_size
            await persist_position(position)

    # ── Disconnect & Reconnect ────────────────────────────────

    async def _handle_disconnect(self) -> None:
        """
        On WebSocket disconnect:
        1. Mark all active positions as RECONNECTING
        2. Start reconnection loop
        3. On success → full state sync → restore positions
        """
        self._connected = False
        self._warmed_up = False

        # Transition all active positions to RECONNECTING
        for pos in self._positions.values():
            if pos.state in (PositionState.OPEN, PositionState.ADJUSTING):
                pos.state_machine.transition(
                    TransitionTrigger.WS_DISCONNECTED,
                    reason="WebSocket connection lost"
                )
                await persist_position(pos)

        # Reconnection loop
        while self._reconnect_attempts < self._max_reconnect_attempts:
            backoff = min(
                self._base_backoff * (2 ** self._reconnect_attempts),
                60.0  # max 60s between attempts
            )
            await asyncio.sleep(backoff)
            self._reconnect_attempts += 1

            try:
                await self.start()
                await self._full_state_sync()
                return
            except Exception as e:
                logger.warning(f"Reconnect attempt {self._reconnect_attempts} failed: {e}")

        # Max retries exceeded — emergency close all positions
        await self._emergency_close_all("Max WS reconnect attempts exceeded")

    async def _full_state_sync(self) -> None:
        """
        After reconnection, sync local state with exchange reality.

        CRITICAL: While we were disconnected, anything could have happened:
        - SL/TP could have triggered
        - Position could have been liquidated
        - Partial fills could have occurred

        This runs DURING warmup — it uses REST API (not WS ticks)
        so warmup gate doesn't block it.
        """
        for symbol, local_pos in list(self._positions.items()):
            exchange_pos = await self.adapter.get_position(symbol)

            if exchange_pos is None or exchange_pos.size == 0:
                # Position was closed while disconnected
                local_pos.state_machine.transition(
                    TransitionTrigger.ALL_CLOSED,
                    reason="Position closed during disconnect"
                )
                local_pos.state = PositionState.CLOSED
                await persist_position(local_pos)
                continue

            # Sync size (might have changed due to partial fills)
            if abs(exchange_pos.size) != local_pos.current_quantity:
                local_pos.current_quantity = abs(exchange_pos.size)

            # Seed price cache from REST snapshot (avoids stale tick
            # false positives when first WS tick arrives after warmup)
            self._last_prices[symbol] = exchange_pos.mark_price

            # Check if our SL/TP orders still exist
            open_orders = await self.adapter.get_open_conditional_orders(symbol)
            sl_exists = any(o.order_type == "stop_loss" for o in open_orders)
            tp_exists = any(o.order_type == "take_profit" for o in open_orders)

            if not sl_exists and local_pos.state != PositionState.CLOSED:
                # SL was triggered or cancelled — re-place immediately
                logger.critical(f"Position {symbol} has NO SL after reconnect! Re-placing...")
                queue = get_order_queue(self.account_id)
                await queue.enqueue(OrderTask(
                    priority=OrderPriority.EMERGENCY_SL,
                    created_at=time.time(),
                    position_id=local_pos.position_id,
                    action="place_sl",
                    params={
                        "trigger_price": local_pos.current_sl_price,
                        "quantity": local_pos.current_quantity,
                    },
                ))

            # Restore to pre-disconnect state
            local_pos.state_machine.transition(
                TransitionTrigger.SYNC_COMPLETE,
                reason="Full state sync after reconnection"
            )
            await persist_position(local_pos)
```

---

## 10. Rate Limit Strategy

### 10.1 Binance Rate Limits

```
┌─────────────────────────────────────────────────┐
│              Binance Futures Limits               │
├─────────────────────────────────────────────────┤
│ IP Weight:        2400 / min                     │
│ Order Count (10s): depends on VIP (default 300)  │
│ Order Count (1m):  depends on VIP (default 1200) │
│ Algo Orders:      200 active across all symbols  │
│                                                   │
│ Per cancel-and-replace SL = 2 order counts       │
│ Per multi-TP setup (3 levels) = 3 order counts   │
│ Worst case per tick: 2 (SL) + 3 (TP) = 5        │
│                                                   │
│ Headers to track:                                 │
│  X-MBX-ORDER-COUNT-10S                           │
│  X-MBX-ORDER-COUNT-1M                            │
│  X-MBX-USED-WEIGHT-1M                            │
│                                                   │
│ On 429: MUST backoff, parse Retry-After           │
│ On 418: IP banned, wait (usually 2-30 min)       │
│                                                   │
│ NOTE: reduce-only and close-position orders are   │
│ EXEMPT from -1008 throttling. SL is safe.        │
└─────────────────────────────────────────────────┘
```

### 10.2 Bybit Rate Limits

```
┌─────────────────────────────────────────────────┐
│              Bybit V5 Limits                      │
├─────────────────────────────────────────────────┤
│ IP:        600 req / 5 sec (all endpoints)       │
│ Create:    10 req/sec per symbol (default)       │
│ Amend:     10 req/sec per symbol                 │
│ Cancel:    10 req/sec per symbol                 │
│ Batch:     separate pool, 1-10 orders/request    │
│ Conditional: max 10 active per symbol            │
│                                                   │
│ Headers:                                          │
│  X-Bapi-Limit-Status                             │
│  X-Bapi-Limit-Reset-Timestamp                    │
│                                                   │
│ ADVANTAGE: Bybit amend-in-place costs 1 req      │
│ vs Binance cancel+place = 2 reqs                 │
│                                                   │
│ For trading-stop: overwrites in-place,            │
│ doesn't count as separate order                  │
│                                                   │
│ On 403: IP banned for 10 min                     │
└─────────────────────────────────────────────────┘
```

### 10.3 Adaptive Rate Limiter

```python
class AdaptiveRateLimiter:
    """
    Tracks rate limit consumption from response headers
    and provides can_proceed() checks before each request.

    Strategy:
    - Use 70% of limit as soft ceiling → slow down
    - Use 90% of limit as hard ceiling → pause new orders
    - SL orders bypass hard ceiling (they're critical)
    - Parse response headers after every exchange request
    """

    def __init__(self, exchange: str):
        self.exchange = exchange
        self._counters: dict[str, int] = {}
        self._limits: dict[str, int] = {}
        self._reset_times: dict[str, float] = {}

    def update_from_headers(self, headers: dict) -> None:
        """Parse rate limit info from exchange response headers."""
        if self.exchange == "binance":
            for key, val in headers.items():
                if "X-MBX-ORDER-COUNT" in key:
                    self._counters[key] = int(val)
                elif "x-mbx-used-weight" in key:
                    self._counters[key] = int(val)
        elif self.exchange == "bybit":
            status = headers.get("X-Bapi-Limit-Status")
            reset = headers.get("X-Bapi-Limit-Reset-Timestamp")
            if status:
                self._counters["remaining"] = int(status)
            if reset:
                self._reset_times["main"] = int(reset) / 1000

    def can_proceed(self, is_sl: bool = False) -> tuple[bool, float]:
        """
        Returns (can_proceed, wait_seconds).
        SL orders always proceed (is_sl=True).
        """
        if is_sl:
            return True, 0.0

        usage_pct = self._get_usage_pct()

        if usage_pct >= 0.9:
            wait = self._time_until_reset()
            return False, wait
        elif usage_pct >= 0.7:
            # Slow down: add small delay
            return True, 0.2

        return True, 0.0
```

---

## 11. Стратегический профиль (конфигурация на уровне пользователя)

### 11.1 Schema

```python
class StrategyProfileConfig(BaseModel):
    """
    User-configurable strategy parameters for auto-trade.
    Stored in auto_trade_config or a new strategy_profiles table.
    """
    # SL/TP base
    sl_mode: Literal["fixed", "atr", "percentage"] = "fixed"
    sl_value: float                # price, ATR multiplier, or %
    tp_mode: Literal["single", "multi"] = "single"
    tp_value: Optional[float] = None       # For single TP
    tp_levels: Optional[list[dict]] = None  # For multi TP

    # Trailing
    trailing_enabled: bool = False
    trailing_callback_rate: float = 1.0     # %
    trailing_activation_offset: Optional[float] = None  # Activate after X% profit

    # Breakeven
    breakeven_enabled: bool = False
    breakeven_trigger_rr: float = 1.0

    # Volatility SL
    volatility_sl_enabled: bool = False
    volatility_atr_period: int = 14
    volatility_atr_multiplier: float = 2.0

    # Watchers
    watchers: list[WatcherConfig] = []

    # Adjustment priority
    adjustment_priority: list[str] = ["watcher", "trailing", "breakeven", "volatility"]

    # Position management
    max_position_pct: float = 100.0  # Max % of balance per position
    allow_sl_widen: bool = False     # Whether volatility can widen SL
```

---

## 12. Миграция существующего кода

### 12.1 Что сохраняется

- `exchange_trade_ledger` — синхронизация fills с биржи (расширяется, не переписывается)
- `exchange_order_metadata` — provenance map (расширяется полем `order_category: "entry" | "sl" | "tp" | "trailing"`)
- `exchange_trade_sync_state` — high-water marks (без изменений)
- `auto_trade_config` — конфигурация пользователя (расширяется `strategy_profile_json`)
- REST API endpoints для auto-trade — сохраняются, добавляются новые
- Taskiq scheduler infrastructure — переиспользуется для watcher tasks

### 12.2 Что рефакторится

- `auto_trade_positions` — расширяется новыми полями (state machine, SL/TP history, watchers)
- Код размещения ордеров — оборачивается в `ExchangeAdapter` layer
- Код sync задачи — должен понимать algo orders (Binance) и partial TP (Bybit)
- `client_order_id` generation — расширяется для различения SL/TP/entry/trailing

### 12.3 Что добавляется нового

- `ExchangeAdapter` + `BinanceAdapter` + `BybitAdapter`
- `PositionStateMachine` + `PositionContext`
- `OrderExecutionQueue` (priority-based)
- `SLAdjustmentPipeline` (trailing + breakeven + volatility + watchers)
- `MultiTPEngine`
- `IndicatorWatcher` + Taskiq periodic task
- `WebSocketManager` с auto-reconnect и state sync
- `AdaptiveRateLimiter`
- Alembic migration для расширения `auto_trade_positions`
- Redis pub/sub для watcher event bus

### 12.4 Новая структура файлов в Trade Service

```
app/
├── services/
│   ├── exchange/
│   │   ├── __init__.py
│   │   ├── adapter.py              # ExchangeAdapter ABC
│   │   ├── binance_adapter.py      # Binance implementation
│   │   ├── bybit_adapter.py        # Bybit implementation
│   │   ├── factory.py              # AdapterFactory
│   │   └── rate_limiter.py         # AdaptiveRateLimiter
│   ├── position/
│   │   ├── __init__.py
│   │   ├── state_machine.py        # PositionStateMachine
│   │   ├── context.py              # PositionContext
│   │   ├── order_queue.py          # OrderExecutionQueue
│   │   └── reconciliation.py       # State sync logic
│   ├── sl_tp/
│   │   ├── __init__.py
│   │   ├── pipeline.py             # SLAdjustmentPipeline
│   │   ├── trailing.py             # Trailing stop logic
│   │   ├── breakeven.py            # Breakeven logic
│   │   ├── volatility.py           # Volatility-based SL
│   │   └── multi_tp.py             # MultiTPEngine
│   ├── watchers/
│   │   ├── __init__.py
│   │   ├── indicator_watcher.py    # IndicatorWatcher
│   │   ├── rule_engine.py          # Condition evaluation
│   │   └── event_bus.py            # Redis pub/sub watcher events
│   ├── ws/
│   │   ├── __init__.py
│   │   └── manager.py              # WebSocketManager
│   └── auto_trade.py               # Existing (refactored orchestrator)
├── worker/
│   ├── tasks.py                    # Existing + new watcher tasks
│   └── scheduler.py                # Existing + watcher scheduling
├── models/
│   └── auto_trade.py               # Extended SQLAlchemy models
└── schemas/
    └── strategy_profile.py         # StrategyProfileConfig
```

---

## 13. Критические edge cases и их обработка

### 13.1 "Unprotected Position" window

**Проблема:** На Binance при cancel-and-replace SL есть окно, когда позиция без SL.

**Решение:**

1. Проверить новую цену SL ДО отмены старого ордера
2. Cancel старый + НЕМЕДЛЕННО place новый (не ждать подтверждения cancel)
3. Если новый placement вернул ошибку → retry 3 раза с 500ms
4. Если всё равно failed → emergency market close
5. На Bybit проблемы нет: `trading-stop` атомарно перезаписывает SL

### 13.2 Partial fill during SL shift

**Проблема:** Пока мы меняем SL, старый SL мог partially trigger.

**Решение:**

1. Перед cancel проверить статус ордера (не TRIGGERING/TRIGGERED)
2. На Binance: проверить `ALGO_UPDATE` status != "TRIGGERING"
3. Если SL в процессе срабатывания — НЕ ОТМЕНЯТЬ, дождаться fill
4. Подписка на WS гарантирует обнаружение такой ситуации

### 13.3 Position closed externally

**Проблема:** Юзер закрыл позицию через UI биржи.

**Решение:**

1. `ACCOUNT_UPDATE` / position topic покажет size=0
2. WebSocketManager обнаруживает и переводит в CLOSED
3. Отменить все оставшиеся conditional orders
4. Bybit auto-cancels TP/SL при закрытии позиции
5. Binance algo orders с `GTE_GTC` тоже auto-cancel при закрытии

### 13.4 Liquidation

**Проблема:** Позиция ликвидирована биржей.

**Решение:**

1. Обнаруживается через `ACCOUNT_UPDATE` (m="ADMIN" или m="MARGIN_CALL")
2. Все conditional orders автоматически отменяются биржей
3. Перевести позицию в CLOSED с reason="liquidation"
4. Alert пользователю

### 13.5 Multiple positions on same symbol

**Проблема:** В hedge mode возможны LONG и SHORT на один символ.

**Решение:**

1. `PositionContext` всегда содержит `position_side` (LONG/SHORT/BOTH)
2. На Binance: `positionSide` в каждом запросе
3. На Bybit: `positionIdx` (0=one-way, 1=buy/long, 2=sell/short)
4. Exchange adapter принимает `position_side` и транслирует

### 13.6 Stale/anomalous data causing false SL/TP action

**Проблема:** После reconnect или при деградации соединения WS может отдать
кэшированный снапшот или аномальный тик. Trailing engine видит ложный пик,
сдвигает SL, и позиция закрывается по невалидной цене.

**Решение (три слоя в WebSocketManager):**

1. **Warmup gate** — первые 4 секунды после (ре)коннекта тики обновляют кэш цен,
   но НЕ передаются в watcher/adjustment pipeline. REST state sync идёт параллельно
   и не блокируется warmup.
2. **Stale tick guard** — дельта цены с предыдущим тиком сравнивается с порогом
   (2% BTC, 5% alt). Аномальный тик отбрасывается и логируется.
3. **Jitter EMA** — если интервалы heartbeat деградируют, proactive reconnect
   срабатывает ДО потери данных. Seed'им price cache из REST snapshot при sync,
   чтобы первый тик после warmup не вызвал ложный stale reject.

---

## 14. Порядок реализации (рекомендуемый)

### Phase 1: Foundation (Exchange Adapter + State Machine)

1. `ExchangeAdapter` ABC + `BinanceAdapter` + `BybitAdapter`
2. `PositionStateMachine` + unit tests всех transitions
3. `PositionContext` + Alembic migration
4. Рефакторинг текущего auto-trade: оборачивание в adapter

### Phase 2: Dynamic SL/TP

5. `OrderExecutionQueue` с приоритетами
6. `SLAdjustmentPipeline` (trailing + breakeven)
7. Cancel-and-replace logic per exchange
8. Volatility-based SL

### Phase 3: Multi-TP

9. `MultiTPEngine`
10. Partial close execution
11. SL adjustment on TP hit

### Phase 4: Indicator Watchers

12. `IndicatorWatcher` + Taskiq integration
13. Rule engine + event bus (Redis)
14. Watcher → adjustment pipeline integration

### Phase 5: Fault Tolerance

15. `WebSocketManager` с auto-reconnect
16. Data quality guards (warmup gate, stale tick guard, jitter health)
17. Full state sync on reconnection
18. `AdaptiveRateLimiter`
19. Emergency SL / market close escalation

### Phase 6: Testing

20. Unit tests: state machine, adjustment pipeline, multi-TP
21. Integration tests: mock exchange → full lifecycle
22. Testnet smoke tests: real exchange (Binance testnet + Bybit testnet)
