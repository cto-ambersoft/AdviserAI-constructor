"""Bybit V5 Unified Trading adapter."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import inspect
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

import aiohttp
import websockets

from app.schemas.exchange_trading import ExchangeMode
from app.services.exchange.adapter import (
    ConditionalOrderResult,
    EntryOrderResult,
    ExchangeAdapter,
    OrderSide,
    PartialCloseResult,
    PositionSide,
    PositionSnapshot,
    RateLimitState,
)
from app.services.exchange.rate_limiter import AdaptiveRateLimiter

logger = logging.getLogger(__name__)


class BybitAPIError(Exception):
    """Raised when Bybit REST API returns a non-success response."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Bybit API error ({status_code}): {payload}")


class BybitAdapter(ExchangeAdapter):
    """Bybit V5 Unified Trading adapter."""

    _POSITION_TPSL_ORDER_PREFIX = "bybit-position-tpsl"
    _REST_BASE_URL = "https://api.bybit.com"
    _DEMO_REST_BASE_URL = "https://api-demo.bybit.com"
    _PRIVATE_WS_URL = "wss://stream.bybit.com/v5/private"
    _DEMO_PRIVATE_WS_BASE_URL = "wss://stream-demo.bybit.com"
    _PUBLIC_LINEAR_WS_URL = "wss://stream.bybit.com/v5/public/linear"
    _WS_PING_INTERVAL_SECONDS = 20
    _WS_AUTH_EXPIRY_MS = 10_000
    _CCXT_TO_BYBIT_INTERVAL: dict[str, str] = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "2h": "120",
        "4h": "240",
        "6h": "360",
        "12h": "720",
        "1d": "D",
        "1w": "W",
        "1M": "M",
    }
    _BYBIT_TO_CCXT_INTERVAL: dict[str, str] = {
        value: key for key, value in _CCXT_TO_BYBIT_INTERVAL.items()
    }

    def __init__(
        self,
        ccxt_exchange: Any,
        api_key: str,
        api_secret: str,
        rate_limiter: AdaptiveRateLimiter,
        mode: ExchangeMode = "real",
    ) -> None:
        self._ccxt = ccxt_exchange
        self._api_key = api_key
        self._api_secret = api_secret
        self._rate_limiter = rate_limiter
        self._validate_ccxt_environment(ccxt_exchange=ccxt_exchange, mode=mode)
        self._base_url = self._DEMO_REST_BASE_URL if mode == "demo" else self._REST_BASE_URL
        self._PRIVATE_WS_URL = (
            f"{self._DEMO_PRIVATE_WS_BASE_URL}/v5/private"
            if mode == "demo"
            else self._PRIVATE_WS_URL
        )
        self._recv_window_ms = 5_000
        self._request_timeout = aiohttp.ClientTimeout(total=15)
        self._user_data_task: asyncio.Task[None] | None = None
        self._user_data_ping_task: asyncio.Task[None] | None = None
        self._kline_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._rate_state = RateLimitState(
            order_count_10s=0,
            order_count_1m=0,
            order_limit_10s=600,
            order_limit_1m=7200,
            weight_used_1m=0,
            weight_limit_1m=100,
            retry_after=None,
        )

    @classmethod
    def _validate_ccxt_environment(
        cls,
        *,
        ccxt_exchange: Any,
        mode: ExchangeMode,
    ) -> None:
        environment = cls._detect_ccxt_environment(ccxt_exchange)
        if mode == "demo":
            if environment == "demo":
                return
            if environment == "sandbox":
                raise ValueError(
                    "BybitAdapter mode='demo' requires a CCXT exchange configured for "
                    "Bybit demo trading, not sandbox endpoints."
                )
            raise ValueError(
                "BybitAdapter mode='demo' requires a CCXT exchange configured for "
                "Bybit demo trading."
            )

        if environment == "mainnet":
            return
        if environment == "sandbox":
            raise ValueError(
                "BybitAdapter mode='real' cannot use a CCXT exchange configured for "
                "Bybit sandbox endpoints."
            )
        raise ValueError(
            "BybitAdapter mode='real' cannot use a CCXT exchange configured for Bybit demo trading."
        )

    @classmethod
    def _detect_ccxt_environment(cls, ccxt_exchange: Any) -> Literal["mainnet", "sandbox", "demo"]:
        """Detect whether a CCXT client is configured for demo, sandbox or mainnet.

        Authoritative signals only:

        - ``options['enableDemoTrading'] is True`` — set by
          ``ccxt_exchange.enable_demo_trading(True)`` for Bybit demo trading.
        - ``isSandboxModeEnabled is True`` — set by
          ``ccxt_exchange.set_sandbox_mode(True)``; this also swaps
          ``urls['api']`` to ``api-testnet.bybit.com`` endpoints.

        We deliberately do NOT scan ``urls`` for substrings: fresh CCXT
        ``bybit`` clients carry side-by-side ``urls['test']`` and
        ``urls['demotrading']`` sub-trees by default, which produced
        false positives on every real-money client (mirror bug of the
        Binance variant). See regression test
        ``test_fresh_real_ccxt_bybit_detects_as_mainnet``.
        """
        options = getattr(ccxt_exchange, "options", None)
        if isinstance(options, Mapping) and bool(options.get("enableDemoTrading")):
            return "demo"

        sandbox_flag = getattr(ccxt_exchange, "isSandboxModeEnabled", False)
        if isinstance(sandbox_flag, bool) and sandbox_flag:
            return "sandbox"

        return "mainnet"

    async def _fetch_position_payload(
        self,
        symbol: str,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], float] | None:
        positions = await self._ccxt.fetch_positions([symbol])
        if not positions:
            return None

        normalized_symbol = self._normalize_symbol(symbol)
        selected: Mapping[str, Any] | None = None

        for raw_position in positions:
            candidate_symbol = self._normalize_symbol(str(raw_position.get("symbol", "")))
            if candidate_symbol == normalized_symbol:
                selected = raw_position
                break

        if selected is None:
            selected = positions[0]

        info = selected.get("info")
        info_map: Mapping[str, Any] = info if isinstance(info, Mapping) else {}

        raw_size = self._to_optional_float(selected.get("contracts"))
        if raw_size is None:
            raw_size = self._to_optional_float(selected.get("size"))
        if raw_size is None:
            raw_size = self._to_optional_float(info_map.get("size"))
        if raw_size is None:
            raw_size = self._to_optional_float(info_map.get("positionAmt"))
        if raw_size is None:
            raw_size = 0.0

        if raw_size == 0.0:
            return None

        return selected, info_map, raw_size

    @classmethod
    def _position_tpsl_order_id(cls, symbol: str, order_type: str) -> str:
        return f"{cls._POSITION_TPSL_ORDER_PREFIX}:{order_type}:{cls._normalize_symbol(symbol)}"

    @classmethod
    def _position_tpsl_order_type(cls, order_id: str) -> str | None:
        prefix = f"{cls._POSITION_TPSL_ORDER_PREFIX}:"
        if not order_id.startswith(prefix):
            return None
        parts = order_id.split(":")
        if len(parts) < 3:
            return None
        order_type = parts[1].strip().lower()
        if order_type in {"stop_loss", "take_profit", "trailing_stop"}:
            return order_type
        return None

    @classmethod
    def _full_position_protection_orders(
        cls,
        *,
        symbol: str,
        info_map: Mapping[str, Any],
        size: float,
    ) -> list[ConditionalOrderResult]:
        orders: list[ConditionalOrderResult] = []
        for field_name, order_type in (
            ("stopLoss", "stop_loss"),
            ("takeProfit", "take_profit"),
            ("trailingStop", "trailing_stop"),
        ):
            trigger_price = cls._to_float(info_map.get(field_name), default=0.0)
            if trigger_price <= 0:
                continue
            orders.append(
                ConditionalOrderResult(
                    exchange_order_id=cls._position_tpsl_order_id(symbol, order_type),
                    client_order_id="",
                    order_type=order_type,
                    trigger_price=trigger_price,
                    quantity=size,
                    status="new",
                    is_algo=False,
                )
            )
        return orders

    async def _cancel_position_trading_stop(self, symbol: str, order_type: str) -> bool:
        params: dict[str, Any] = {
            "category": "linear",
            "symbol": self._normalize_symbol(symbol),
            "tpslMode": "Full",
            "positionIdx": 0,
        }
        if order_type == "stop_loss":
            params["stopLoss"] = "0"
        elif order_type == "take_profit":
            params["takeProfit"] = "0"
        elif order_type == "trailing_stop":
            params["trailingStop"] = "0"
        else:
            return False

        try:
            await self._signed_request("POST", "/v5/position/trading-stop", params)
            return True
        except BybitAPIError:
            return False

    async def get_position(self, symbol: str) -> PositionSnapshot | None:
        position_payload = await self._fetch_position_payload(symbol)
        if position_payload is None:
            return None

        selected, info_map, raw_size = position_payload

        side = self._position_side(selected, raw_size)
        return PositionSnapshot(
            symbol=symbol,
            side=side,
            size=abs(raw_size),
            entry_price=self._to_float(
                selected.get("entryPrice", info_map.get("avgPrice")),
                default=0.0,
            ),
            unrealized_pnl=self._to_float(
                selected.get("unrealizedPnl", info_map.get("unrealisedPnl")),
                default=0.0,
            ),
            leverage=int(
                self._to_float(
                    selected.get("leverage", info_map.get("leverage")),
                    default=0.0,
                )
            ),
            mark_price=self._to_float(
                selected.get("markPrice", info_map.get("markPrice")),
                default=0.0,
            ),
            liquidation_price=self._to_float(
                selected.get(
                    "liquidationPrice",
                    info_map.get("liqPrice", info_map.get("liquidationPrice")),
                ),
                default=0.0,
            ),
            open_orders=[],
        )

    async def get_open_conditional_orders(self, symbol: str) -> list[ConditionalOrderResult]:
        payload = await self._signed_request(
            "GET",
            "/v5/order/realtime",
            {
                "category": "linear",
                "symbol": self._normalize_symbol(symbol),
                "openOnly": 0,
            },
        )

        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        result_map_raw = payload_map.get("result")
        result_map: Mapping[str, Any] = (
            result_map_raw if isinstance(result_map_raw, Mapping) else {}
        )
        raw_orders_raw = result_map.get("list")
        raw_orders: list[Any] = raw_orders_raw if isinstance(raw_orders_raw, list) else []

        result: list[ConditionalOrderResult] = []
        seen_keys: set[tuple[str, float]] = set()
        for item in raw_orders:
            if not isinstance(item, Mapping):
                continue
            order_type = self._normalize_order_type(item)
            if order_type == "unknown":
                continue
            trigger_price = self._to_float(item.get("triggerPrice"), default=0.0)
            if trigger_price == 0.0 and order_type == "take_profit":
                trigger_price = self._to_float(item.get("takeProfit"), default=0.0)
            if trigger_price == 0.0 and order_type == "stop_loss":
                trigger_price = self._to_float(item.get("stopLoss"), default=0.0)
            status = self._normalize_status(item.get("orderStatus", "new"))
            if status != "new":
                continue

            result.append(
                ConditionalOrderResult(
                    exchange_order_id=str(item.get("orderId", "")),
                    client_order_id=str(item.get("orderLinkId", "")),
                    order_type=order_type,
                    trigger_price=trigger_price,
                    quantity=self._to_float(
                        item.get("qty", item.get("orderQty", item.get("leavesQty"))),
                        default=0.0,
                    ),
                    status=status,
                    is_algo=False,
                )
            )
            seen_keys.add((order_type, trigger_price))

        position_payload = await self._fetch_position_payload(symbol)
        if position_payload is None:
            return result

        _, info_map, raw_size = position_payload
        for position_order in self._full_position_protection_orders(
            symbol=symbol,
            info_map=info_map,
            size=abs(raw_size),
        ):
            dedupe_key = (position_order.order_type, position_order.trigger_price)
            if dedupe_key in seen_keys:
                continue
            result.append(position_order)
        return result

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
        amount_str = await self._amount_to_precision(symbol, quantity)
        params: dict[str, Any] = {
            "reduceOnly": False,
            "orderLinkId": client_order_id,
            "clientOrderId": client_order_id,
        }
        if stop_loss_price is not None:
            params["stopLoss"] = await self._price_to_precision(symbol, stop_loss_price)
            params["slTriggerBy"] = "MarkPrice"
            params["slOrderType"] = "Market"
        if take_profit_price is not None:
            params["takeProfit"] = await self._price_to_precision(symbol, take_profit_price)
            params["tpTriggerBy"] = "MarkPrice"
            params["tpOrderType"] = "Market"

        order = await self._ccxt.create_order(
            symbol,
            "market",
            side.value,
            float(amount_str),
            None,
            params=params,
        )
        timestamp_raw = order.get("timestamp")
        timestamp = (
            datetime.fromtimestamp(int(timestamp_raw) / 1000.0, tz=UTC)
            if isinstance(timestamp_raw, (int, float)) and float(timestamp_raw) > 0
            else None
        )
        client_id = order.get("clientOrderId") or order.get("orderLinkId") or client_order_id
        price = self._to_optional_float(order.get("price"))
        average_price = self._to_optional_float(order.get("average"))
        filled_quantity = self._to_float(order.get("filled"), default=float(quantity))

        attached_sl = self._build_attached_protection(
            symbol=symbol,
            order_type="stop_loss",
            trigger_price=stop_loss_price,
            quantity=filled_quantity,
            client_order_id=sl_client_order_id,
        )
        attached_tp = self._build_attached_protection(
            symbol=symbol,
            order_type="take_profit",
            trigger_price=take_profit_price,
            quantity=filled_quantity,
            client_order_id=tp_client_order_id,
        )

        return EntryOrderResult(
            exchange_order_id=str(order.get("id", order.get("orderId", ""))),
            client_order_id=str(client_id),
            symbol=str(order.get("symbol", symbol)),
            side=side,
            order_type=str(order.get("type", "market")).lower(),
            status=self._normalize_status(order.get("status", "closed")),
            quantity=self._to_float(order.get("amount"), default=float(quantity)),
            filled_quantity=filled_quantity,
            remaining_quantity=self._to_float(order.get("remaining"), default=0.0),
            price=price,
            average_price=average_price,
            cost=self._to_optional_float(order.get("cost")),
            timestamp=timestamp,
            raw=order if isinstance(order, dict) else {},
            attached_sl=attached_sl,
            attached_tp=attached_tp,
        )

    def _build_attached_protection(
        self,
        *,
        symbol: str,
        order_type: str,
        trigger_price: float | None,
        quantity: float,
        client_order_id: str | None,
    ) -> ConditionalOrderResult | None:
        if trigger_price is None:
            return None
        return ConditionalOrderResult(
            exchange_order_id=self._position_tpsl_order_id(symbol, order_type),
            client_order_id=client_order_id or "",
            order_type=order_type,
            trigger_price=float(trigger_price),
            quantity=float(quantity),
            status="new",
            is_algo=False,
        )

    async def place_stop_loss(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        reduce_only: bool = True,
        close_position: bool = False,
    ) -> ConditionalOrderResult:
        # Bybit's ``tpslMode: "Full"`` is already equivalent to Binance's
        # ``closePosition=true`` — the SL covers the entire position at
        # trigger time. The flag is accepted purely for interface parity.
        _ = (side, reduce_only, close_position)
        trigger_str = await self._price_to_precision(symbol, trigger_price)
        payload = await self._signed_request(
            "POST",
            "/v5/position/trading-stop",
            {
                "category": "linear",
                "symbol": self._normalize_symbol(symbol),
                "tpslMode": "Full",
                "stopLoss": trigger_str,
                "slTriggerBy": "MarkPrice",
                "slOrderType": "Market",
                "positionIdx": 0,
                # Pass our client order id so the resulting Bybit conditional
                # echoes it back via WS execution events as ``orderLinkId``.
                # Without this, the only way to map a TP/SL fill to a level is
                # via price-tolerance heuristics.
                "orderLinkId": client_order_id,
            },
        )
        return self._conditional_result_from_payload(
            payload=payload,
            order_type="stop_loss",
            trigger_price=float(trigger_str),
            quantity=quantity,
            client_order_id=client_order_id,
            default_exchange_order_id=self._position_tpsl_order_id(symbol, "stop_loss"),
        )

    async def place_take_profit(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        reduce_only: bool = True,
        limit_price: float | None = None,
    ) -> ConditionalOrderResult:
        _ = (side, reduce_only)
        tp_order_type = "Limit" if limit_price is not None else "Market"
        trigger_str = await self._price_to_precision(symbol, trigger_price)
        qty_str = await self._amount_to_precision(symbol, quantity)
        params: dict[str, Any] = {
            "category": "linear",
            "symbol": self._normalize_symbol(symbol),
            "takeProfit": trigger_str,
            "tpTriggerBy": "MarkPrice",
            "tpslMode": "Partial",
            "tpSize": qty_str,
            # Bybit requires matching TP/SL sizes for Partial trading-stop requests.
            "slSize": qty_str,
            "tpOrderType": tp_order_type,
            "positionIdx": 0,
            # See place_stop_loss for the rationale on orderLinkId.
            "orderLinkId": client_order_id,
        }
        if limit_price is not None:
            params["tpLimitPrice"] = await self._price_to_precision(symbol, limit_price)

        payload = await self._signed_request("POST", "/v5/position/trading-stop", params)
        return self._conditional_result_from_payload(
            payload=payload,
            order_type="take_profit",
            trigger_price=float(trigger_str),
            quantity=float(qty_str),
            client_order_id=client_order_id,
            default_exchange_order_id=client_order_id,
        )

    async def place_trailing_stop(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        callback_rate: float,
        activation_price: float | None,
        client_order_id: str,
    ) -> ConditionalOrderResult:
        _ = side
        params: dict[str, Any] = {
            "category": "linear",
            "symbol": self._normalize_symbol(symbol),
            "tpslMode": "Full",
            "trailingStop": self._format_number(callback_rate),
            "positionIdx": 0,
        }
        if activation_price is not None:
            params["activePrice"] = await self._price_to_precision(symbol, activation_price)

        payload = await self._signed_request("POST", "/v5/position/trading-stop", params)
        return self._conditional_result_from_payload(
            payload=payload,
            order_type="trailing_stop",
            trigger_price=activation_price or 0.0,
            quantity=quantity,
            client_order_id=client_order_id,
            default_exchange_order_id=self._position_tpsl_order_id(symbol, "trailing_stop"),
        )

    async def cancel_and_replace_sl(
        self,
        symbol: str,
        existing_order_id: str,
        new_trigger_price: float,
        new_quantity: float,
        client_order_id: str,
        close_position: bool = True,
    ) -> ConditionalOrderResult:
        """Atomically move the position SL to a new trigger price.

        Bybit's ``/v5/position/trading-stop`` mutates the position-level SL,
        so ``existing_order_id`` is only needed for the rare ``/v5/order/amend``
        fallback.

        ``close_position=True`` (default) sends ``tpslMode: "Full"`` and omits
        ``slSize`` so the SL closes whatever position remains at trigger time
        — matching Binance ``closePosition=true`` semantics. This is the
        correct mode for multi-TP profiles where partial fills shrink the
        live position automatically. ``close_position=False`` keeps the
        legacy ``Partial`` mode plus an explicit ``slSize`` for the
        trailing/breakeven flows that target a specific slice.
        """
        trigger_str = await self._price_to_precision(symbol, new_trigger_price)
        request: dict[str, Any] = {
            "category": "linear",
            "symbol": self._normalize_symbol(symbol),
            "stopLoss": trigger_str,
            "slTriggerBy": "MarkPrice",
            "slOrderType": "Market",
            "positionIdx": 0,
            "orderLinkId": client_order_id,
        }
        qty_str: str | None = None
        if close_position:
            request["tpslMode"] = "Full"
        else:
            qty_str = await self._amount_to_precision(symbol, new_quantity)
            request["tpslMode"] = "Partial"
            request["slSize"] = qty_str
        try:
            payload = await self._signed_request(
                "POST",
                "/v5/position/trading-stop",
                request,
            )
            return self._conditional_result_from_payload(
                payload=payload,
                order_type="stop_loss",
                trigger_price=float(trigger_str),
                quantity=float(qty_str) if qty_str is not None else float(new_quantity),
                client_order_id=client_order_id,
                default_exchange_order_id=self._position_tpsl_order_id(symbol, "stop_loss"),
            )
        except BybitAPIError:
            amend_qty = (
                qty_str
                if qty_str is not None
                else await self._amount_to_precision(symbol, new_quantity)
            )
            payload = await self._signed_request(
                "POST",
                "/v5/order/amend",
                {
                    "category": "linear",
                    "symbol": self._normalize_symbol(symbol),
                    "orderId": existing_order_id,
                    "orderLinkId": client_order_id,
                    "triggerPrice": trigger_str,
                    "qty": amend_qty,
                },
            )
            return self._conditional_result_from_payload(
                payload=payload,
                order_type="stop_loss",
                trigger_price=float(trigger_str),
                quantity=float(amend_qty),
                client_order_id=client_order_id,
                default_exchange_order_id=existing_order_id or client_order_id,
            )

    async def cancel_and_replace_tp(
        self,
        symbol: str,
        existing_order_id: str,
        new_trigger_price: float,
        new_quantity: float,
        client_order_id: str,
        limit_price: float | None = None,
    ) -> ConditionalOrderResult:
        tpsl_mode = await self._resolve_tpsl_mode(symbol, existing_order_id)
        if tpsl_mode == "partial":
            await self.cancel_conditional_order(symbol, existing_order_id)
            side = await self._resolve_take_profit_side(symbol)
            return await self.place_take_profit(
                symbol=symbol,
                side=side,
                quantity=new_quantity,
                trigger_price=new_trigger_price,
                client_order_id=client_order_id,
                reduce_only=True,
                limit_price=limit_price,
            )

        trigger_str = await self._price_to_precision(symbol, new_trigger_price)
        payload = await self._signed_request(
            "POST",
            "/v5/position/trading-stop",
            {
                "category": "linear",
                "symbol": self._normalize_symbol(symbol),
                "tpslMode": "Full",
                "takeProfit": trigger_str,
                "tpTriggerBy": "MarkPrice",
                "tpOrderType": "Market",
                "positionIdx": 0,
            },
        )
        return self._conditional_result_from_payload(
            payload=payload,
            order_type="take_profit",
            trigger_price=float(trigger_str),
            quantity=new_quantity,
            client_order_id=client_order_id,
            default_exchange_order_id=self._position_tpsl_order_id(symbol, "take_profit"),
        )

    async def cancel_conditional_order(self, symbol: str, order_id: str) -> bool:
        position_order_type = self._position_tpsl_order_type(order_id)
        if position_order_type is not None:
            return await self._cancel_position_trading_stop(symbol, position_order_type)

        try:
            await self._signed_request(
                "POST",
                "/v5/order/cancel",
                {
                    "category": "linear",
                    "symbol": self._normalize_symbol(symbol),
                    "orderId": order_id,
                },
            )
            return True
        except BybitAPIError:
            return False

    async def clear_symbol_conditional_orders(self, symbol: str) -> None:
        """Best-effort cleanup for Bybit symbol TP/SL state and open conditional orders."""
        normalized_symbol = self._normalize_symbol(symbol)

        # Full-mode TP/SL/trailing stop live on the position itself.
        with contextlib.suppress(BybitAPIError):
            await self._signed_request(
                "POST",
                "/v5/position/trading-stop",
                {
                    "category": "linear",
                    "symbol": normalized_symbol,
                    "tpslMode": "Full",
                    "takeProfit": "0",
                    "stopLoss": "0",
                    "trailingStop": "0",
                    "positionIdx": 0,
                },
            )

        # Bybit cancel-all for linear clears active, conditional, TP/SL and trailing-stop orders
        # when no orderFilter is specified.
        with contextlib.suppress(BybitAPIError):
            await self._signed_request(
                "POST",
                "/v5/order/cancel-all",
                {
                    "category": "linear",
                    "symbol": normalized_symbol,
                },
            )

    async def partial_close(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        client_order_id: str,
        order_type: str = "market",
        price: float | None = None,
    ) -> PartialCloseResult:
        normalized_type = order_type.lower()
        amount_str = await self._amount_to_precision(symbol, quantity)
        order_price: float | None = None
        if normalized_type == "limit" and price is not None:
            order_price = float(await self._price_to_precision(symbol, price))
        order = await self._ccxt.create_order(
            symbol,
            normalized_type,
            side.value,
            float(amount_str),
            order_price,
            params={
                "reduceOnly": True,
                "orderLinkId": client_order_id,
                "clientOrderId": client_order_id,
            },
        )

        executed_qty = self._to_float(order.get("filled"), default=0.0)
        avg_price = self._to_float(order.get("average", order.get("price")), default=0.0)
        remaining_default = max(0.0, quantity - executed_qty)
        remaining_qty = self._to_float(order.get("remaining"), default=remaining_default)
        fee = order.get("fee")
        commission = 0.0
        if isinstance(fee, Mapping):
            commission = self._to_float(fee.get("cost"), default=0.0)

        return PartialCloseResult(
            executed_qty=executed_qty,
            avg_price=avg_price,
            remaining_qty=remaining_qty,
            order_id=str(order.get("id", order.get("orderId", ""))),
            commission=commission,
        )

    async def subscribe_user_data(
        self,
        on_order_update: Callable[..., Any],
        on_position_update: Callable[..., Any],
        on_disconnect: Callable[..., Any],
    ) -> None:
        await self._stop_user_data_stream()

        self._user_data_task = self._start_background_task(
            self._run_user_data_stream(
                on_order_update=on_order_update,
                on_position_update=on_position_update,
                on_disconnect=on_disconnect,
            ),
            name="bybit-user-data",
            cleanup=lambda task: self._clear_user_data_task(task),
        )

    async def subscribe_kline(
        self,
        symbol: str,
        interval: str,
        on_kline: Callable[..., Any],
    ) -> None:
        normalized_symbol = self._normalize_symbol(symbol)
        topic = f"kline.{self._to_bybit_kline_interval(interval)}.{normalized_symbol}"
        await self._stop_kline_stream(topic)

        task = self._start_background_task(
            self._run_kline_stream(topic=topic, on_kline=on_kline),
            name=f"bybit-kline-{topic}",
            cleanup=lambda current: self._clear_kline_task(topic, current),
        )
        self._kline_tasks[topic] = task

    async def get_rate_limit_state(self) -> RateLimitState:
        return replace(self._rate_state)

    async def can_place_order(self) -> bool:
        can_proceed, _ = self._rate_limiter.can_proceed()
        return can_proceed

    async def _resolve_tpsl_mode(
        self,
        symbol: str,
        existing_order_id: str,
    ) -> Literal["partial", "full"]:
        if self._position_tpsl_order_type(existing_order_id) is not None:
            return "full"

        try:
            payload = await self._signed_request(
                "GET",
                "/v5/order/realtime",
                {
                    "category": "linear",
                    "symbol": self._normalize_symbol(symbol),
                    "orderId": existing_order_id,
                    "openOnly": 1,
                },
            )
        except BybitAPIError:
            return "full"

        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        result_map_raw = payload_map.get("result")
        result_map: Mapping[str, Any] = (
            result_map_raw if isinstance(result_map_raw, Mapping) else {}
        )
        raw_orders_raw = result_map.get("list")
        raw_orders: list[Any] = raw_orders_raw if isinstance(raw_orders_raw, list) else []

        for item in raw_orders:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("orderId", "")) != existing_order_id:
                continue
            tpsl_mode = str(item.get("tpslMode", "")).strip().lower()
            if tpsl_mode == "partial":
                return "partial"
            if tpsl_mode == "full":
                return "full"

        return "full"

    async def _resolve_take_profit_side(self, symbol: str) -> OrderSide:
        try:
            position = await self.get_position(symbol)
        except Exception:
            return OrderSide.SELL

        if position is not None and position.side == PositionSide.SHORT:
            return OrderSide.BUY
        return OrderSide.SELL

    async def _signed_request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        method_upper = method.upper()
        request_params: dict[str, Any] = {}
        if params:
            request_params = {k: v for k, v in params.items() if v is not None}

        timestamp = str(int(time.time() * 1000))
        recv_window = str(self._recv_window_ms)

        encoded_query = ""
        encoded_body = ""
        if method_upper == "GET":
            encoded_query = urlencode(self._stringify_params(request_params))
            payload_for_sign = encoded_query
        else:
            encoded_body = json.dumps(request_params, separators=(",", ":"), sort_keys=True)
            payload_for_sign = encoded_body

        signature = self._sign(
            timestamp=timestamp,
            recv_window=recv_window,
            payload=payload_for_sign,
        )
        headers: dict[str, str] = {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        if method_upper != "GET":
            headers["Content-Type"] = "application/json"

        url = f"{self._base_url}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"

        request_data = encoded_body if method_upper != "GET" else None
        async with aiohttp.ClientSession(timeout=self._request_timeout) as session:
            async with session.request(
                method_upper,
                url,
                headers=headers,
                data=request_data,
            ) as response:
                raw_headers = dict(response.headers)
                self._update_rate_limit_state(raw_headers)
                payload = await self._decode_response(response)
                if response.status >= 400 or self._is_error_payload(payload):
                    raise BybitAPIError(response.status, payload)
                return payload

    def _sign(self, *, timestamp: str, recv_window: str, payload: str) -> str:
        raw = f"{timestamp}{self._api_key}{recv_window}{payload}"
        return hmac.new(
            self._api_secret.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _decode_response(self, response: aiohttp.ClientResponse) -> Any:
        body = await response.text()
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"raw": body}

    def _update_rate_limit_state(self, headers: Mapping[str, Any]) -> None:
        self._rate_limiter.update_from_headers(dict(headers))
        lower_headers = {str(key).lower(): value for key, value in headers.items()}
        next_state = replace(self._rate_state)

        limit = self._to_int(lower_headers.get("x-bapi-limit"))
        remaining = self._to_int(lower_headers.get("x-bapi-limit-status"))
        retry_after = self._to_optional_float(lower_headers.get("retry-after"))
        reset_timestamp_ms = self._to_optional_float(
            lower_headers.get("x-bapi-limit-reset-timestamp")
        )

        if limit is not None:
            next_state.weight_limit_1m = limit
        if remaining is not None:
            configured_limit = next_state.weight_limit_1m
            inferred_limit = configured_limit if configured_limit > 0 else remaining
            next_state.weight_used_1m = max(inferred_limit - remaining, 0)
        if retry_after is not None and retry_after > 0:
            next_state.retry_after = retry_after
        elif reset_timestamp_ms is not None:
            next_state.retry_after = max(0.0, (reset_timestamp_ms / 1000.0) - time.time())

        self._rate_state = next_state

    def _conditional_result_from_payload(
        self,
        *,
        payload: Any,
        order_type: str,
        trigger_price: float,
        quantity: float,
        client_order_id: str,
        default_exchange_order_id: str,
    ) -> ConditionalOrderResult:
        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        result_map_raw = payload_map.get("result")
        result_map: Mapping[str, Any] = (
            result_map_raw if isinstance(result_map_raw, Mapping) else {}
        )
        exchange_order_id = str(
            result_map.get(
                "orderId",
                payload_map.get("orderId", default_exchange_order_id),
            )
        )
        if not exchange_order_id:
            exchange_order_id = default_exchange_order_id

        status_source = result_map.get("orderStatus", payload_map.get("retMsg", "new"))
        return ConditionalOrderResult(
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
            order_type=order_type,
            trigger_price=trigger_price,
            quantity=quantity,
            status=self._normalize_status(status_source),
            is_algo=False,
        )

    @staticmethod
    def _is_error_payload(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        code = payload.get("retCode")
        if code is None:
            return False
        try:
            numeric_code = int(str(code))
        except (TypeError, ValueError):
            return False
        return numeric_code != 0

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = symbol.split(":")[0].replace("/", "").strip()
        return normalized.upper()

    @classmethod
    def _normalize_order_type(cls, order: Mapping[str, Any]) -> str:
        stop_order_type = str(order.get("stopOrderType", "")).strip().lower()
        if "takeprofit" in stop_order_type:
            return "take_profit"
        if "stoploss" in stop_order_type:
            return "stop_loss"
        if "trailing" in stop_order_type:
            return "trailing_stop"

        # Fallback: infer from explicit tp/sl fields if exchange omits stopOrderType.
        take_profit = cls._to_optional_float(order.get("takeProfit"))
        stop_loss = cls._to_optional_float(order.get("stopLoss"))
        if take_profit is not None and take_profit > 0:
            return "take_profit"
        if stop_loss is not None and stop_loss > 0:
            return "stop_loss"
        return "unknown"

    @staticmethod
    def _normalize_status(raw: Any) -> str:
        mapping = {
            "OK": "new",
            "NEW": "new",
            "UNTRIGGERED": "new",
            "ACTIVE": "new",
            "PARTIALLYFILLED": "new",
            "TRIGGERED": "triggered",
            "FILLED": "triggered",
            "CANCELED": "cancelled",
            "CANCELLED": "cancelled",
            "DEACTIVATED": "cancelled",
            "REJECTED": "rejected",
        }
        normalized = mapping.get(str(raw).upper())
        if normalized is not None:
            return normalized
        return str(raw).lower() if raw is not None else "new"

    @staticmethod
    def _position_side(position: Mapping[str, Any], raw_size: float) -> PositionSide:
        raw_side = str(position.get("side", position.get("positionSide", ""))).lower()
        if raw_side in {"long", "buy"}:
            return PositionSide.LONG
        if raw_side in {"short", "sell"}:
            return PositionSide.SHORT
        if raw_size > 0:
            return PositionSide.LONG
        if raw_size < 0:
            return PositionSide.SHORT
        return PositionSide.BOTH

    @classmethod
    def _stringify_params(cls, params: Mapping[str, Any]) -> dict[str, str]:
        encoded: dict[str, str] = {}
        for key, value in params.items():
            if isinstance(value, bool):
                encoded[key] = str(value).lower()
            elif isinstance(value, float):
                encoded[key] = cls._format_number(value)
            else:
                encoded[key] = str(value)
        return encoded

    @staticmethod
    def _format_number(value: float) -> str:
        return format(value, ".15g")

    async def _ensure_markets_loaded(self) -> None:
        """Lazily load CCXT markets metadata (required for precision formatting)."""
        if not getattr(self._ccxt, "markets", None):
            await self._ccxt.load_markets()

    async def _amount_to_precision(self, symbol: str, amount: float) -> str:
        """Round quantity to the symbol's lot-size step."""
        await self._ensure_markets_loaded()
        return str(self._ccxt.amount_to_precision(symbol, float(amount)))

    async def _price_to_precision(self, symbol: str, price: float) -> str:
        """Round price to the symbol's tick size."""
        await self._ensure_markets_loaded()
        return str(self._ccxt.price_to_precision(symbol, float(price)))

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(str(value).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    async def _run_user_data_stream(
        self,
        *,
        on_order_update: Callable[..., Any],
        on_position_update: Callable[..., Any],
        on_disconnect: Callable[..., Any],
    ) -> None:
        ping_task: asyncio.Task[None] | None = None
        manual_stop = False

        try:
            async with websockets.connect(
                self._PRIVATE_WS_URL,
                ping_interval=None,
                ping_timeout=None,
            ) as websocket:
                await self._authenticate_private_websocket(websocket)
                await websocket.send(
                    json.dumps(
                        {
                            "op": "subscribe",
                            "args": ["order", "execution", "position"],
                        },
                        separators=(",", ":"),
                    )
                )

                ping_task = self._start_background_task(
                    self._run_private_ping(websocket),
                    name="bybit-private-ping",
                    cleanup=lambda task: self._clear_user_data_ping_task(task),
                )
                self._user_data_ping_task = ping_task

                async for raw_message in websocket:
                    payload = self._parse_ws_payload(raw_message)
                    if payload is None:
                        continue

                    await self._dispatch_private_payload(
                        payload=payload,
                        on_order_update=on_order_update,
                        on_position_update=on_position_update,
                    )
        except asyncio.CancelledError:
            manual_stop = True
            raise
        except Exception:
            logger.exception("Bybit private WebSocket stream failed.")
        finally:
            if ping_task is not None:
                await self._cancel_task(ping_task)

            if not manual_stop:
                await self._invoke_callback(on_disconnect)

    async def _authenticate_private_websocket(self, websocket: Any) -> None:
        expires = int(time.time() * 1000) + self._WS_AUTH_EXPIRY_MS
        signature = self._sign_ws_auth(expires)
        await websocket.send(
            json.dumps(
                {"op": "auth", "args": [self._api_key, expires, signature]},
                separators=(",", ":"),
            )
        )

        while True:
            payload = self._parse_ws_payload(await websocket.recv())
            if payload is None:
                continue

            if str(payload.get("op", "")).lower() != "auth":
                continue

            ret_code = self._to_int(payload.get("retCode"))
            success = payload.get("success")
            if success is False or (ret_code is not None and ret_code != 0):
                raise BybitAPIError(401, dict(payload))
            return

    def _sign_ws_auth(self, expires: int) -> str:
        payload = f"GET/realtime{expires}"
        return hmac.new(
            self._api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _dispatch_private_payload(
        self,
        *,
        payload: Mapping[str, Any],
        on_order_update: Callable[..., Any],
        on_position_update: Callable[..., Any],
    ) -> None:
        op = str(payload.get("op", "")).lower()
        if op in {"auth", "subscribe", "ping", "pong"}:
            ret_code = self._to_int(payload.get("retCode"))
            if ret_code is not None and ret_code != 0:
                raise BybitAPIError(400, dict(payload))
            return

        topic = str(payload.get("topic", "")).strip().lower()
        if not topic:
            return

        if topic == "order":
            for event in self._normalize_order_events(payload):
                await self._invoke_callback(on_order_update, event)
            return

        if topic == "execution":
            for event in self._normalize_execution_events(payload):
                await self._invoke_callback(on_order_update, event)
            return

        if topic == "position":
            for event in self._normalize_position_events(payload):
                await self._invoke_callback(on_position_update, event)

    def _normalize_order_events(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        topic_data = payload.get("data")
        raw_items = topic_data if isinstance(topic_data, list) else []
        events: list[dict[str, Any]] = []
        creation_time = self._to_int(payload.get("creationTime"))

        for item in raw_items:
            if not isinstance(item, Mapping):
                continue

            order_type = self._normalize_ws_order_type(item)
            events.append(
                {
                    "type": "order",
                    "topic": "order",
                    "event_time": creation_time,
                    "transaction_time": self._to_int(
                        item.get("updatedTime", item.get("createdTime"))
                    ),
                    "symbol": self._normalize_symbol(str(item.get("symbol", ""))),
                    "order_id": str(item.get("orderId", "")),
                    "client_order_id": str(item.get("orderLinkId", "")),
                    "status": self._normalize_status(item.get("orderStatus", "new")),
                    "raw_status": str(item.get("orderStatus", "")),
                    "order_type": order_type,
                    "raw_order_type": str(item.get("orderType", "")),
                    "price": self._to_float(
                        item.get("triggerPrice", item.get("price")),
                        default=0.0,
                    ),
                    "trigger_price": self._resolve_order_trigger_price(item),
                    "average_price": self._to_float(item.get("avgPrice"), default=0.0),
                    "quantity": self._to_float(item.get("qty"), default=0.0),
                    "filled_quantity": self._to_float(item.get("cumExecQty"), default=0.0),
                    "remaining_quantity": self._to_float(item.get("leavesQty"), default=0.0),
                    "side": str(item.get("side", "")).lower(),
                    "reduce_only": bool(item.get("reduceOnly", False)),
                    "close_on_trigger": bool(item.get("closeOnTrigger", False)),
                    "is_algo": order_type in {"stop_loss", "take_profit", "trailing_stop"},
                    "raw": dict(item),
                }
            )

        return events

    def _normalize_execution_events(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        topic_data = payload.get("data")
        raw_items = topic_data if isinstance(topic_data, list) else []
        events: list[dict[str, Any]] = []
        creation_time = self._to_int(payload.get("creationTime"))

        for item in raw_items:
            if not isinstance(item, Mapping):
                continue

            order_type = self._normalize_ws_order_type(item)
            exec_qty = self._to_float(item.get("execQty"), default=0.0)
            leaves_qty = self._to_float(item.get("leavesQty"), default=0.0)
            status = "triggered" if exec_qty > 0 and leaves_qty <= 0 else "new"
            events.append(
                {
                    "type": "execution",
                    "topic": "execution",
                    "event_time": creation_time,
                    "transaction_time": self._to_int(item.get("execTime")),
                    "symbol": self._normalize_symbol(str(item.get("symbol", ""))),
                    "order_id": str(item.get("orderId", "")),
                    "client_order_id": str(item.get("orderLinkId", "")),
                    "status": status,
                    "raw_status": str(item.get("execType", "")),
                    "execution_type": str(item.get("execType", "")).lower(),
                    "order_type": order_type,
                    "raw_order_type": str(item.get("orderType", "")),
                    "price": self._to_float(
                        item.get("execPrice", item.get("orderPrice")),
                        default=0.0,
                    ),
                    "trigger_price": self._resolve_order_trigger_price(item),
                    "quantity": exec_qty,
                    "filled_quantity": exec_qty,
                    "remaining_quantity": leaves_qty,
                    "order_quantity": self._to_float(item.get("orderQty"), default=0.0),
                    "side": str(item.get("side", "")).lower(),
                    "mark_price": self._to_float(item.get("markPrice"), default=0.0),
                    "exec_fee": self._to_float(item.get("execFee"), default=0.0),
                    "is_algo": order_type in {"stop_loss", "take_profit", "trailing_stop"},
                    "raw": dict(item),
                }
            )

        return events

    def _normalize_position_events(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        topic_data = payload.get("data")
        raw_items = topic_data if isinstance(topic_data, list) else []
        events: list[dict[str, Any]] = []
        creation_time = self._to_int(payload.get("creationTime"))

        for item in raw_items:
            if not isinstance(item, Mapping):
                continue

            side = str(item.get("side", "")).lower()
            size = self._to_float(item.get("size"), default=0.0)
            signed_amount = -abs(size) if side == "sell" else abs(size)
            events.append(
                {
                    "type": "position",
                    "topic": "position",
                    "event_time": creation_time,
                    "transaction_time": self._to_int(
                        item.get("updatedTime", item.get("createdTime"))
                    ),
                    "symbol": self._normalize_symbol(str(item.get("symbol", ""))),
                    "size": abs(size),
                    "position_amount": signed_amount,
                    "side": side,
                    "entry_price": self._to_float(item.get("avgPrice"), default=0.0),
                    "mark_price": self._to_float(item.get("markPrice"), default=0.0),
                    "liquidation_price": self._to_float(item.get("liqPrice"), default=0.0),
                    "leverage": self._to_int(item.get("leverage")),
                    "unrealized_pnl": self._to_float(item.get("unrealisedPnl"), default=0.0),
                    "position_status": str(item.get("positionStatus", "")).lower(),
                    "take_profit": self._to_float(item.get("takeProfit"), default=0.0),
                    "stop_loss": self._to_float(item.get("stopLoss"), default=0.0),
                    "trailing_stop": self._to_float(item.get("trailingStop"), default=0.0),
                    "raw": dict(item),
                }
            )

        return events

    async def _run_private_ping(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(self._WS_PING_INTERVAL_SECONDS)
            await websocket.send(json.dumps({"op": "ping"}, separators=(",", ":")))

    async def _run_kline_stream(
        self,
        *,
        topic: str,
        on_kline: Callable[..., Any],
    ) -> None:
        try:
            async with websockets.connect(
                self._PUBLIC_LINEAR_WS_URL,
                ping_interval=None,
                ping_timeout=None,
            ) as websocket:
                await websocket.send(
                    json.dumps({"op": "subscribe", "args": [topic]}, separators=(",", ":"))
                )

                async for raw_message in websocket:
                    payload = self._parse_ws_payload(raw_message)
                    if payload is None:
                        continue

                    op = str(payload.get("op", "")).lower()
                    if op in {"subscribe", "pong"}:
                        ret_code = self._to_int(payload.get("retCode"))
                        if ret_code is not None and ret_code != 0:
                            raise BybitAPIError(400, dict(payload))
                        continue

                    event = self._normalize_kline_event(payload)
                    if event is None:
                        continue

                    await self._invoke_callback(on_kline, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Bybit public kline stream failed for %s.", topic)

    @staticmethod
    def _parse_ws_payload(raw_message: Any) -> Mapping[str, Any] | None:
        if isinstance(raw_message, bytes):
            raw_text = raw_message.decode("utf-8")
        else:
            raw_text = str(raw_message)

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("Failed to decode Bybit WebSocket message: %r", raw_message)
            return None

        if not isinstance(payload, Mapping):
            return None
        return payload

    @classmethod
    def _to_bybit_kline_interval(cls, interval: str) -> str:
        normalized = interval.strip()
        mapped = cls._CCXT_TO_BYBIT_INTERVAL.get(normalized)
        if mapped is None:
            raise ValueError(f"Unsupported Bybit kline interval: {interval!r}")
        return mapped

    @classmethod
    def _from_bybit_kline_interval(cls, interval: str) -> str:
        normalized = interval.strip()
        return cls._BYBIT_TO_CCXT_INTERVAL.get(normalized, normalized)

    def _normalize_kline_event(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        topic = str(payload.get("topic", "")).strip()
        if not topic.startswith("kline."):
            return None

        topic_data = payload.get("data")
        raw_items = topic_data if isinstance(topic_data, list) else []
        if not raw_items:
            return None

        item = raw_items[0]
        if not isinstance(item, Mapping):
            return None

        symbol = str(item.get("symbol", "")) or topic.rsplit(".", 1)[-1]
        raw_interval = str(item.get("interval", ""))
        return {
            "type": "kline",
            "topic": topic,
            "event_time": self._to_int(payload.get("ts")),
            "symbol": self._normalize_symbol(symbol),
            "interval": self._from_bybit_kline_interval(raw_interval),
            "open_time": self._to_int(item.get("start")),
            "close_time": self._to_int(item.get("end")),
            "open": self._to_float(item.get("open"), default=0.0),
            "high": self._to_float(item.get("high"), default=0.0),
            "low": self._to_float(item.get("low"), default=0.0),
            "close": self._to_float(item.get("close"), default=0.0),
            "volume": self._to_float(item.get("volume"), default=0.0),
            "quote_volume": self._to_float(item.get("turnover"), default=0.0),
            "is_closed": bool(item.get("confirm", False)),
            "last_trade_timestamp": self._to_int(item.get("timestamp")),
            "raw": dict(item),
        }

    @classmethod
    def _normalize_ws_order_type(cls, payload: Mapping[str, Any]) -> str:
        normalized = cls._normalize_order_type(payload)
        if normalized != "unknown":
            return normalized

        raw_order_type = str(payload.get("orderType", "")).strip().lower()
        if raw_order_type:
            return raw_order_type
        return "unknown"

    @classmethod
    def _resolve_order_trigger_price(cls, payload: Mapping[str, Any]) -> float:
        candidates = (
            payload.get("triggerPrice"),
            payload.get("stopLoss"),
            payload.get("takeProfit"),
            payload.get("price"),
            payload.get("orderPrice"),
        )
        for candidate in candidates:
            parsed = cls._to_optional_float(candidate)
            if parsed is not None and parsed > 0:
                return parsed
        return 0.0

    async def _invoke_callback(self, callback: Callable[..., Any], *args: Any) -> Any:
        result = callback(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    def _start_background_task(
        self,
        coroutine: Any,
        *,
        name: str,
        cleanup: Callable[[asyncio.Task[Any]], None] | None = None,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(lambda current: self._background_tasks.discard(current))
        task.add_done_callback(self._build_task_logger(name))
        if cleanup is not None:
            task.add_done_callback(cleanup)
        return task

    def _build_task_logger(self, task_name: str) -> Callable[[asyncio.Task[Any]], None]:
        def _callback(task: asyncio.Task[Any]) -> None:
            if task.cancelled():
                return

            try:
                exc = task.exception()
            except Exception:
                logger.exception("Failed to inspect Bybit task %s.", task_name)
                return

            if exc is not None:
                logger.error(
                    "Bybit background task %s failed.",
                    task_name,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        return _callback

    async def _stop_user_data_stream(self) -> None:
        user_data_task = self._user_data_task
        self._user_data_task = None
        await self._cancel_task(user_data_task)

        ping_task = self._user_data_ping_task
        self._user_data_ping_task = None
        await self._cancel_task(ping_task)

    async def _stop_kline_stream(self, topic: str) -> None:
        task = self._kline_tasks.pop(topic, None)
        await self._cancel_task(task)

    async def _cancel_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _clear_user_data_task(self, task: asyncio.Task[Any]) -> None:
        if self._user_data_task is task:
            self._user_data_task = None

    def _clear_user_data_ping_task(self, task: asyncio.Task[Any]) -> None:
        if self._user_data_ping_task is task:
            self._user_data_ping_task = None

    def _clear_kline_task(self, topic: str, task: asyncio.Task[Any]) -> None:
        current = self._kline_tasks.get(topic)
        if current is task:
            self._kline_tasks.pop(topic, None)
