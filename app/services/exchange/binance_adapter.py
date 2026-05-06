"""Binance USDT-M Futures adapter with Algo Orders API support."""

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
from typing import Any
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


class BinanceAPIError(Exception):
    """Raised when Binance REST API returns a non-success response."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Binance API error ({status_code}): {payload}")


class CriticalSLPlacementError(Exception):
    """Raised when SL replacement fails after emergency retries."""


class BinanceAdapter(ExchangeAdapter):
    """Binance USDT-M Futures adapter."""

    _REST_BASE_URL = "https://fapi.binance.com"
    _DEMO_REST_BASE_URL = "https://demo-fapi.binance.com"
    _PRIVATE_WS_BASE_URL = "wss://fstream.binance.com/ws"
    _DEMO_PRIVATE_WS_BASE_URL = "wss://demo-fstream.binance.com/ws"
    _PUBLIC_MARKET_WS_BASE_URL = "wss://fstream.binance.com/ws"
    _LISTEN_KEY_KEEPALIVE_SECONDS = 30 * 60

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
        self._user_data_ws_base_url = (
            self._DEMO_PRIVATE_WS_BASE_URL if mode == "demo" else self._PRIVATE_WS_BASE_URL
        )
        self._market_ws_base_url = self._PUBLIC_MARKET_WS_BASE_URL
        self._time_offset_ms = 0
        self._recv_window_ms = 5_000
        self._request_timeout = aiohttp.ClientTimeout(total=15)
        self._listen_key: str | None = None
        self._user_data_task: asyncio.Task[None] | None = None
        self._user_data_keepalive_task: asyncio.Task[None] | None = None
        self._kline_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._rate_state = RateLimitState(
            order_count_10s=0,
            order_count_1m=0,
            order_limit_10s=300,
            order_limit_1m=1200,
            weight_used_1m=0,
            weight_limit_1m=2400,
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
                    "BinanceAdapter mode='demo' requires a CCXT exchange configured for "
                    "Binance demo trading, not sandbox endpoints."
                )
            raise ValueError(
                "BinanceAdapter mode='demo' requires a CCXT exchange configured for "
                "Binance demo trading."
            )

        if environment == "mainnet":
            return
        if environment == "demo":
            raise ValueError(
                "BinanceAdapter mode='real' cannot use a CCXT exchange configured for "
                "Binance demo trading."
            )
        raise ValueError(
            "BinanceAdapter mode='real' cannot use a CCXT exchange configured for "
            "Binance sandbox endpoints."
        )

    @classmethod
    def _detect_ccxt_environment(cls, ccxt_exchange: Any) -> str:
        options = getattr(ccxt_exchange, "options", None)
        if isinstance(options, Mapping) and bool(options.get("enableDemoTrading")):
            return "demo"

        sandbox_flag = getattr(ccxt_exchange, "isSandboxModeEnabled", False)
        if isinstance(sandbox_flag, bool) and sandbox_flag:
            return "sandbox"

        urls = getattr(ccxt_exchange, "urls", None)
        for candidate in cls._iter_string_values(urls):
            normalized = candidate.lower()
            if "demo-fapi." in normalized or "demo-fstream." in normalized:
                return "demo"
            if "testnet" in normalized or "binancefuture.com" in normalized:
                return "sandbox"

        return "mainnet"

    @classmethod
    def _iter_string_values(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, Mapping):
            values: list[str] = []
            for nested in value.values():
                values.extend(cls._iter_string_values(nested))
            return values
        if isinstance(value, (list, tuple, set)):
            nested_values: list[str] = []
            for nested in value:
                nested_values.extend(cls._iter_string_values(nested))
            return nested_values
        return []

    async def get_position(self, symbol: str) -> PositionSnapshot | None:
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
            raw_size = self._to_float(info_map.get("positionAmt"), default=0.0)

        if raw_size == 0.0:
            return None

        side = self._position_side(selected, raw_size)
        return PositionSnapshot(
            symbol=symbol,
            side=side,
            size=abs(raw_size),
            entry_price=self._to_float(selected.get("entryPrice"), default=0.0),
            unrealized_pnl=self._to_float(selected.get("unrealizedPnl"), default=0.0),
            leverage=int(self._to_float(selected.get("leverage"), default=0.0)),
            mark_price=self._to_float(selected.get("markPrice"), default=0.0),
            liquidation_price=self._to_float(
                selected.get("liquidationPrice", info_map.get("liquidationPrice")),
                default=0.0,
            ),
            open_orders=[],
        )

    async def get_open_conditional_orders(self, symbol: str) -> list[ConditionalOrderResult]:
        payload = await self._signed_request(
            "GET",
            "/fapi/v1/openAlgoOrders",
            {"symbol": self._normalize_symbol(symbol)},
        )

        raw_orders: list[Any] = []
        if isinstance(payload, list):
            raw_orders = payload
        elif isinstance(payload, Mapping):
            maybe_data = payload.get("data")
            if isinstance(maybe_data, list):
                raw_orders = maybe_data

        result: list[ConditionalOrderResult] = []
        for item in raw_orders:
            if not isinstance(item, Mapping):
                continue
            order_type = self._normalize_order_type(item.get("type", item.get("orderType", "")))
            result.append(
                ConditionalOrderResult(
                    exchange_order_id=str(item.get("algoId", "")),
                    client_order_id=str(item.get("clientAlgoId", item.get("newClientAlgoId", ""))),
                    order_type=order_type,
                    trigger_price=self._to_float(
                        item.get("triggerPrice", item.get("stopPrice", item.get("activatePrice"))),
                        default=0.0,
                    ),
                    quantity=self._to_float(item.get("quantity"), default=0.0),
                    status=self._normalize_status(
                        item.get("algoStatus", item.get("status", "new"))
                    ),
                    is_algo=True,
                )
            )
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
        order = await self._ccxt.create_order(
            symbol,
            "market",
            side.value,
            float(amount_str),
            None,
            params={"reduceOnly": False, "newClientOrderId": client_order_id},
        )
        timestamp_raw = order.get("timestamp")
        timestamp = (
            datetime.fromtimestamp(int(timestamp_raw) / 1000.0, tz=UTC)
            if isinstance(timestamp_raw, (int, float)) and float(timestamp_raw) > 0
            else None
        )
        client_id = order.get("clientOrderId") or order.get("newClientOrderId") or client_order_id
        price = self._to_optional_float(order.get("price"))
        average_price = self._to_optional_float(order.get("average"))
        filled_quantity = self._to_float(order.get("filled"), default=float(quantity))

        result = EntryOrderResult(
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
        )

        if take_profit_price is None and stop_loss_price is None:
            return result
        if filled_quantity <= 0:
            # Entry didn't fill; nothing to protect.
            return result

        protective_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        attached_sl: ConditionalOrderResult | None = None
        attached_tp: ConditionalOrderResult | None = None
        try:
            if stop_loss_price is not None:
                attached_sl = await self.place_stop_loss(
                    symbol=symbol,
                    side=protective_side,
                    quantity=filled_quantity,
                    trigger_price=stop_loss_price,
                    client_order_id=sl_client_order_id or f"{client_order_id}-sl",
                    reduce_only=True,
                )
            if take_profit_price is not None:
                attached_tp = await self.place_take_profit(
                    symbol=symbol,
                    side=protective_side,
                    quantity=filled_quantity,
                    trigger_price=take_profit_price,
                    client_order_id=tp_client_order_id or f"{client_order_id}-tp",
                    reduce_only=True,
                )
        except Exception:
            await self._rollback_entry(
                symbol=symbol,
                protective_side=protective_side,
                quantity=filled_quantity,
                attached_sl=attached_sl,
                client_order_id=client_order_id,
            )
            raise

        result.attached_sl = attached_sl
        result.attached_tp = attached_tp
        return result

    async def _rollback_entry(
        self,
        *,
        symbol: str,
        protective_side: OrderSide,
        quantity: float,
        attached_sl: ConditionalOrderResult | None,
        client_order_id: str,
    ) -> None:
        """Best-effort flatten of just-opened position when bracket attach failed."""
        if attached_sl is not None and attached_sl.exchange_order_id:
            try:
                await self.cancel_conditional_order(symbol, attached_sl.exchange_order_id)
            except Exception:
                logger.exception(
                    "Failed to cancel attached SL during rollback for %s (algo_id=%s).",
                    symbol,
                    attached_sl.exchange_order_id,
                )
        try:
            await self.partial_close(
                symbol=symbol,
                side=protective_side,
                quantity=quantity,
                client_order_id=f"{client_order_id}-rb",
                order_type="market",
            )
        except Exception:
            logger.critical(
                "Bracket rollback failed: position remains open and unprotected on %s.",
                symbol,
                exc_info=True,
            )

    async def place_stop_loss(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        trigger_price: float,
        client_order_id: str,
        reduce_only: bool = True,
    ) -> ConditionalOrderResult:
        qty_str = await self._amount_to_precision(symbol, quantity)
        trigger_str = await self._price_to_precision(symbol, trigger_price)
        payload = await self._signed_request(
            "POST",
            "/fapi/v1/algoOrder",
            self._algo_order_params(
                symbol=symbol,
                side=side,
                quantity=qty_str,
                order_type="STOP_MARKET",
                client_order_id=client_order_id,
                trigger_price=trigger_str,
                reduce_only=reduce_only,
                price_protect=True,
            ),
        )
        return self._conditional_result_from_payload(
            payload=payload,
            order_type="stop_loss",
            trigger_price=float(trigger_str),
            quantity=float(qty_str),
            client_order_id=self._client_algo_id(client_order_id),
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
        _ = limit_price
        qty_str = await self._amount_to_precision(symbol, quantity)
        trigger_str = await self._price_to_precision(symbol, trigger_price)
        payload = await self._signed_request(
            "POST",
            "/fapi/v1/algoOrder",
            self._algo_order_params(
                symbol=symbol,
                side=side,
                quantity=qty_str,
                order_type="TAKE_PROFIT_MARKET",
                client_order_id=client_order_id,
                trigger_price=trigger_str,
                reduce_only=reduce_only,
                price_protect=True,
            ),
        )
        return self._conditional_result_from_payload(
            payload=payload,
            order_type="take_profit",
            trigger_price=float(trigger_str),
            quantity=float(qty_str),
            client_order_id=self._client_algo_id(client_order_id),
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
        qty_str = await self._amount_to_precision(symbol, quantity)
        activation_str = (
            await self._price_to_precision(symbol, activation_price)
            if activation_price is not None
            else None
        )
        payload = await self._signed_request(
            "POST",
            "/fapi/v1/algoOrder",
            self._algo_order_params(
                symbol=symbol,
                side=side,
                quantity=qty_str,
                order_type="TRAILING_STOP_MARKET",
                client_order_id=client_order_id,
                callback_rate=callback_rate,
                activation_price=activation_str,
                reduce_only=True,
                price_protect=True,
            ),
        )
        return self._conditional_result_from_payload(
            payload=payload,
            order_type="trailing_stop",
            trigger_price=float(activation_str) if activation_str is not None else 0.0,
            quantity=float(qty_str),
            client_order_id=self._client_algo_id(client_order_id),
        )

    async def cancel_and_replace_sl(
        self,
        symbol: str,
        existing_order_id: str,
        new_trigger_price: float,
        new_quantity: float,
        client_order_id: str,
    ) -> ConditionalOrderResult:
        stop_side = await self._resolve_stop_loss_side(symbol)
        await self._signed_request(
            "DELETE",
            "/fapi/v1/algoOrder",
            {
                "symbol": self._normalize_symbol(symbol),
                "algoId": existing_order_id,
            },
        )

        last_error: BinanceAPIError | None = None
        for attempt in range(4):
            try:
                return await self.place_stop_loss(
                    symbol=symbol,
                    side=stop_side,
                    quantity=new_quantity,
                    trigger_price=new_trigger_price,
                    client_order_id=client_order_id,
                    reduce_only=True,
                )
            except BinanceAPIError as exc:
                last_error = exc
                if attempt == 3:
                    break
                await asyncio.sleep(0.5)

        raise CriticalSLPlacementError(
            "Unable to place replacement stop-loss after 3 retries."
        ) from last_error

    async def cancel_and_replace_tp(
        self,
        symbol: str,
        existing_order_id: str,
        new_trigger_price: float,
        new_quantity: float,
        client_order_id: str,
        limit_price: float | None = None,
    ) -> ConditionalOrderResult:
        await self._signed_request(
            "DELETE",
            "/fapi/v1/algoOrder",
            {
                "symbol": self._normalize_symbol(symbol),
                "algoId": existing_order_id,
            },
        )
        return await self.place_take_profit(
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=new_quantity,
            trigger_price=new_trigger_price,
            client_order_id=client_order_id,
            reduce_only=True,
            limit_price=limit_price,
        )

    async def cancel_conditional_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._signed_request(
                "DELETE",
                "/fapi/v1/algoOrder",
                {
                    "symbol": self._normalize_symbol(symbol),
                    "algoId": order_id,
                },
            )
            return True
        except BinanceAPIError:
            return False

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
            params={"reduceOnly": True, "newClientOrderId": client_order_id},
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

        listen_key = await self._create_listen_key()
        self._listen_key = listen_key
        self._user_data_task = self._start_background_task(
            self._run_user_data_stream(
                listen_key=listen_key,
                on_order_update=on_order_update,
                on_position_update=on_position_update,
                on_disconnect=on_disconnect,
            ),
            name=f"binance-user-data-{listen_key[:8]}",
            cleanup=lambda task: self._clear_user_data_task(task),
        )

    async def subscribe_kline(
        self,
        symbol: str,
        interval: str,
        on_kline: Callable[..., Any],
    ) -> None:
        stream_name = f"{self._normalize_symbol(symbol).lower()}@kline_{interval}"
        await self._stop_kline_stream(stream_name)

        task = self._start_background_task(
            self._run_kline_stream(stream_name=stream_name, on_kline=on_kline),
            name=f"binance-kline-{stream_name}",
            cleanup=lambda current: self._clear_kline_task(stream_name, current),
        )
        self._kline_tasks[stream_name] = task

    async def get_rate_limit_state(self) -> RateLimitState:
        return replace(self._rate_state)

    async def can_place_order(self) -> bool:
        can_proceed, _ = self._rate_limiter.can_proceed()
        return can_proceed

    async def _resolve_stop_loss_side(self, symbol: str) -> OrderSide:
        try:
            position = await self.get_position(symbol)
        except Exception:
            return OrderSide.SELL

        if position is not None and position.side == PositionSide.SHORT:
            return OrderSide.BUY
        return OrderSide.SELL

    def _algo_order_params(
        self,
        *,
        symbol: str,
        side: OrderSide,
        quantity: str | float,
        order_type: str,
        client_order_id: str,
        trigger_price: str | float | None = None,
        callback_rate: str | float | None = None,
        activation_price: str | float | None = None,
        reduce_only: bool,
        price_protect: bool,
    ) -> dict[str, Any]:
        client_algo_id = self._client_algo_id(client_order_id)
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": self._normalize_symbol(symbol),
            "side": side.value.upper(),
            "positionSide": "BOTH",
            "type": order_type,
            "quantity": self._coerce_numeric_str(quantity),
            "timeInForce": "GTE_GTC",
            "workingType": "CONTRACT_PRICE",
            "priceProtect": price_protect,
            "reduceOnly": reduce_only,
            "newClientAlgoId": client_algo_id,
            "clientAlgoId": client_algo_id,
        }
        if trigger_price is not None:
            formatted_trigger = self._coerce_numeric_str(trigger_price)
            params["triggerPrice"] = formatted_trigger
            params["stopPrice"] = formatted_trigger
        if callback_rate is not None:
            params["callbackRate"] = self._coerce_numeric_str(callback_rate)
        if activation_price is not None:
            params["activatePrice"] = self._coerce_numeric_str(activation_price)
        return params

    @classmethod
    def _coerce_numeric_str(cls, value: str | float) -> str:
        return value if isinstance(value, str) else cls._format_number(value)

    async def _signed_request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        signed: bool = True,
    ) -> Any:
        for attempt in range(2):
            request_params: dict[str, Any] = {}
            if params:
                request_params = {k: v for k, v in params.items() if v is not None}

            if signed:
                request_params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
                request_params.setdefault("recvWindow", self._recv_window_ms)

            encoded = urlencode(self._stringify_params(request_params))
            if signed:
                signature = hmac.new(
                    self._api_secret.encode("utf-8"),
                    encoded.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                encoded = (
                    f"{encoded}&signature={signature}" if encoded else f"signature={signature}"
                )

            url = f"{self._base_url}{path}"
            if encoded:
                url = f"{url}?{encoded}"

            headers = {"X-MBX-APIKEY": self._api_key}
            async with aiohttp.ClientSession(timeout=self._request_timeout) as session:
                async with session.request(method.upper(), url, headers=headers) as response:
                    raw_headers = dict(response.headers)
                    self._update_rate_limit_state(raw_headers)
                    payload = await self._decode_response(response)
                    if response.status >= 400 or self._is_error_payload(payload):
                        if signed and attempt == 0 and self._is_timestamp_error(payload):
                            await self._sync_server_time_offset()
                            continue
                        raise BinanceAPIError(response.status, payload)
                    return payload

        raise BinanceAPIError(500, {"msg": "Binance request failed after timestamp sync retry."})

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

        order_count_10s = self._to_int(lower_headers.get("x-mbx-order-count-10s"))
        order_count_1m = self._to_int(lower_headers.get("x-mbx-order-count-1m"))
        weight_used_1m = self._to_int(lower_headers.get("x-mbx-used-weight-1m"))
        retry_after = self._to_optional_float(lower_headers.get("retry-after"))

        if order_count_10s is not None:
            next_state.order_count_10s = order_count_10s
        if order_count_1m is not None:
            next_state.order_count_1m = order_count_1m
        if weight_used_1m is not None:
            next_state.weight_used_1m = weight_used_1m
        if retry_after is not None:
            next_state.retry_after = retry_after

        self._rate_state = next_state

    def _conditional_result_from_payload(
        self,
        *,
        payload: Any,
        order_type: str,
        trigger_price: float,
        quantity: float,
        client_order_id: str,
    ) -> ConditionalOrderResult:
        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        return ConditionalOrderResult(
            exchange_order_id=str(payload_map.get("algoId", payload_map.get("orderId", ""))),
            client_order_id=str(payload_map.get("clientAlgoId", client_order_id)),
            order_type=order_type,
            trigger_price=trigger_price,
            quantity=quantity,
            status=self._normalize_status(payload_map.get("algoStatus", "new")),
            is_algo=True,
        )

    @staticmethod
    def _is_error_payload(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        code = payload.get("code")
        if code is None:
            return False
        try:
            numeric_code = int(str(code))
        except (TypeError, ValueError):
            return False
        return numeric_code < 0

    @staticmethod
    def _is_timestamp_error(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        try:
            return int(str(payload.get("code"))) == -1021
        except (TypeError, ValueError):
            return False

    async def _sync_server_time_offset(self) -> None:
        payload = await self._signed_request("GET", "/fapi/v1/time", signed=False)
        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        server_time = self._to_int(payload_map.get("serverTime"))
        if server_time is None:
            return
        self._time_offset_ms = server_time - int(time.time() * 1000)

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = symbol.split(":")[0].replace("/", "").strip()
        return normalized.upper()

    @staticmethod
    def _client_algo_id(client_order_id: str) -> str:
        raw = "".join(ch for ch in client_order_id if ch.isalnum() or ch in "._:-/") or "algo"
        return raw[:36]

    @staticmethod
    def _position_side(position: Mapping[str, Any], raw_size: float) -> PositionSide:
        raw_side = str(position.get("side", "")).lower()
        if raw_side in {"long", "buy"}:
            return PositionSide.LONG
        if raw_side in {"short", "sell"}:
            return PositionSide.SHORT
        if raw_size > 0:
            return PositionSide.LONG
        if raw_size < 0:
            return PositionSide.SHORT
        return PositionSide.BOTH

    @staticmethod
    def _normalize_order_type(raw: Any) -> str:
        mapping = {
            "STOP_MARKET": "stop_loss",
            "STOP": "stop_loss",
            "TAKE_PROFIT_MARKET": "take_profit",
            "TAKE_PROFIT": "take_profit",
            "TRAILING_STOP_MARKET": "trailing_stop",
        }
        return mapping.get(str(raw).upper(), "unknown")

    @staticmethod
    def _normalize_status(raw: Any) -> str:
        mapping = {
            "NEW": "new",
            "TRIGGERING": "new",
            "TRIGGERED": "triggered",
            "FINISHED": "triggered",
            "CANCELED": "cancelled",
            "CANCELLED": "cancelled",
            "REJECTED": "rejected",
            "EXPIRED": "cancelled",
        }
        normalized = mapping.get(str(raw).upper())
        if normalized is not None:
            return normalized
        return str(raw).lower() if raw is not None else "new"

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
        """Round quantity down to the symbol's stepSize (LOT_SIZE filter)."""
        await self._ensure_markets_loaded()
        return str(self._ccxt.amount_to_precision(symbol, float(amount)))

    async def _price_to_precision(self, symbol: str, price: float) -> str:
        """Round price to the symbol's tickSize (PRICE_FILTER)."""
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

    async def _create_listen_key(self) -> str:
        payload = await self._signed_request("POST", "/fapi/v1/listenKey", signed=False)
        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        listen_key = str(payload_map.get("listenKey", "")).strip()
        if not listen_key:
            raise BinanceAPIError(500, {"msg": "Binance did not return a listenKey."})
        return listen_key

    async def _run_user_data_stream(
        self,
        *,
        listen_key: str,
        on_order_update: Callable[..., Any],
        on_position_update: Callable[..., Any],
        on_disconnect: Callable[..., Any],
    ) -> None:
        uri = f"{self._user_data_ws_base_url}/{listen_key}"
        keepalive_task: asyncio.Task[None] | None = None
        manual_stop = False

        try:
            # websockets automatically responds to ping control frames with pong frames.
            async with websockets.connect(
                uri,
                ping_interval=None,
                ping_timeout=None,
            ) as websocket:
                keepalive_task = self._start_background_task(
                    self._keepalive_listen_key(listen_key),
                    name=f"binance-listen-key-keepalive-{listen_key[:8]}",
                    cleanup=lambda task: self._clear_keepalive_task(task),
                )
                self._user_data_keepalive_task = keepalive_task

                async for raw_message in websocket:
                    payload = self._parse_ws_payload(raw_message)
                    if payload is None:
                        continue

                    should_continue = await self._dispatch_user_data_payload(
                        payload=payload,
                        on_order_update=on_order_update,
                        on_position_update=on_position_update,
                    )
                    if not should_continue:
                        break
        except asyncio.CancelledError:
            manual_stop = True
            raise
        except Exception:
            logger.exception("Binance user data stream failed for listenKey %s.", listen_key)
        finally:
            if keepalive_task is not None:
                await self._cancel_task(keepalive_task)

            if not manual_stop:
                await self._invoke_callback(on_disconnect)

    async def _dispatch_user_data_payload(
        self,
        *,
        payload: Mapping[str, Any],
        on_order_update: Callable[..., Any],
        on_position_update: Callable[..., Any],
    ) -> bool:
        event_type = str(payload.get("e", "")).strip()
        if not event_type:
            return True

        if event_type == "ORDER_TRADE_UPDATE":
            normalized = self._normalize_order_trade_update(payload)
            if normalized is not None:
                await self._invoke_callback(on_order_update, normalized)
            return True

        if event_type == "ALGO_UPDATE":
            normalized = self._normalize_algo_update(payload)
            if normalized is not None:
                await self._invoke_callback(on_order_update, normalized)
            return True

        if event_type == "ACCOUNT_UPDATE":
            for normalized in self._normalize_account_update(payload):
                await self._invoke_callback(on_position_update, normalized)
            return True

        if event_type == "listenKeyExpired":
            logger.warning("Binance listenKey expired for current user data stream.")
            return False

        return True

    async def _keepalive_listen_key(self, listen_key: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._LISTEN_KEY_KEEPALIVE_SECONDS)
                await self._signed_request(
                    "PUT",
                    "/fapi/v1/listenKey",
                    {"listenKey": listen_key},
                    signed=False,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Binance listenKey keepalive failed for %s.", listen_key)

    async def _run_kline_stream(
        self,
        *,
        stream_name: str,
        on_kline: Callable[..., Any],
    ) -> None:
        uri = f"{self._market_ws_base_url}/{stream_name}"
        try:
            async with websockets.connect(
                uri,
                ping_interval=None,
                ping_timeout=None,
            ) as websocket:
                async for raw_message in websocket:
                    payload = self._parse_ws_payload(raw_message)
                    if payload is None:
                        continue

                    normalized = self._normalize_kline_payload(payload)
                    if normalized is None:
                        continue

                    await self._invoke_callback(on_kline, normalized)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Binance kline stream failed for %s.", stream_name)

    @staticmethod
    def _parse_ws_payload(raw_message: Any) -> Mapping[str, Any] | None:
        if isinstance(raw_message, bytes):
            raw_text = raw_message.decode("utf-8")
        else:
            raw_text = str(raw_message)

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("Failed to decode Binance WebSocket message: %r", raw_message)
            return None

        if not isinstance(payload, Mapping):
            return None

        nested = payload.get("data")
        if isinstance(nested, Mapping):
            return nested
        return payload

    def _normalize_order_trade_update(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        order = payload.get("o")
        if not isinstance(order, Mapping):
            return None
        return self._normalize_order_payload(
            payload,
            order_payload=order,
            event_type="ORDER_TRADE_UPDATE",
            is_algo=False,
        )

    def _normalize_algo_update(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        for key in ("ao", "o"):
            order_payload = payload.get(key)
            if isinstance(order_payload, Mapping):
                return self._normalize_order_payload(
                    payload,
                    order_payload=order_payload,
                    event_type="ALGO_UPDATE",
                    is_algo=True,
                )
        return None

    def _normalize_order_payload(
        self,
        payload: Mapping[str, Any],
        *,
        order_payload: Mapping[str, Any],
        event_type: str,
        is_algo: bool,
    ) -> dict[str, Any]:
        raw_type = order_payload.get("o", order_payload.get("ot", ""))
        raw_status = order_payload.get("X", order_payload.get("x", ""))
        return {
            "type": event_type,
            "event_type": event_type,
            "event_time": self._to_int(payload.get("E")),
            "transaction_time": self._to_int(payload.get("T", order_payload.get("T"))),
            "symbol": self._normalize_symbol(str(order_payload.get("s", ""))),
            "order_id": str(order_payload.get("i", order_payload.get("algoId", ""))),
            "client_order_id": str(order_payload.get("c", order_payload.get("clientAlgoId", ""))),
            "status": self._normalize_status(raw_status),
            "raw_status": str(raw_status),
            "execution_type": str(order_payload.get("x", "")),
            "order_type": self._normalize_order_type(raw_type),
            "raw_order_type": str(raw_type),
            "price": self._to_float(
                order_payload.get("sp", order_payload.get("p", order_payload.get("ap"))),
                default=0.0,
            ),
            "average_price": self._to_float(order_payload.get("ap"), default=0.0),
            "last_fill_price": self._to_float(order_payload.get("L"), default=0.0),
            "trigger_price": self._to_float(order_payload.get("sp"), default=0.0),
            "quantity": self._to_float(order_payload.get("q"), default=0.0),
            "filled_quantity": self._to_float(order_payload.get("z"), default=0.0),
            "last_filled_quantity": self._to_float(order_payload.get("l"), default=0.0),
            "side": str(order_payload.get("S", "")).lower(),
            "position_side": str(order_payload.get("ps", "")).lower(),
            "reduce_only": bool(order_payload.get("R", False)),
            "close_position": bool(order_payload.get("cp", False)),
            "is_algo": is_algo,
            "raw": dict(order_payload),
        }

    def _normalize_account_update(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        account = payload.get("a")
        if not isinstance(account, Mapping):
            return []

        raw_positions = account.get("P")
        positions = raw_positions if isinstance(raw_positions, list) else []
        normalized_events: list[dict[str, Any]] = []
        reason = str(account.get("m", ""))
        event_time = self._to_int(payload.get("E"))
        transaction_time = self._to_int(payload.get("T"))

        for item in positions:
            if not isinstance(item, Mapping):
                continue

            raw_amount = self._to_float(item.get("pa"), default=0.0)
            normalized_events.append(
                {
                    "type": "ACCOUNT_UPDATE",
                    "event_type": "ACCOUNT_UPDATE",
                    "event_time": event_time,
                    "transaction_time": transaction_time,
                    "reason": reason,
                    "symbol": self._normalize_symbol(str(item.get("s", ""))),
                    "size": abs(raw_amount),
                    "position_amount": raw_amount,
                    "entry_price": self._to_float(item.get("ep"), default=0.0),
                    "breakeven_price": self._to_float(item.get("bep"), default=0.0),
                    "unrealized_pnl": self._to_float(item.get("up"), default=0.0),
                    "margin_type": str(item.get("mt", "")),
                    "isolated_wallet": self._to_float(item.get("iw"), default=0.0),
                    "position_side": str(item.get("ps", "")).lower(),
                    "raw": dict(item),
                }
            )

        return normalized_events

    def _normalize_kline_payload(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        if str(payload.get("e", "")).strip().lower() != "kline":
            return None

        kline = payload.get("k")
        if not isinstance(kline, Mapping):
            return None

        return {
            "type": "kline",
            "event_time": self._to_int(payload.get("E")),
            "symbol": self._normalize_symbol(str(payload.get("s", kline.get("s", "")))),
            "interval": str(kline.get("i", "")),
            "open_time": self._to_int(kline.get("t")),
            "close_time": self._to_int(kline.get("T")),
            "open": self._to_float(kline.get("o"), default=0.0),
            "high": self._to_float(kline.get("h"), default=0.0),
            "low": self._to_float(kline.get("l"), default=0.0),
            "close": self._to_float(kline.get("c"), default=0.0),
            "volume": self._to_float(kline.get("v"), default=0.0),
            "quote_volume": self._to_float(kline.get("q"), default=0.0),
            "trade_count": self._to_int(kline.get("n")),
            "is_closed": bool(kline.get("x", False)),
            "taker_buy_volume": self._to_float(kline.get("V"), default=0.0),
            "taker_buy_quote_volume": self._to_float(kline.get("Q"), default=0.0),
            "raw": dict(kline),
        }

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
                logger.exception("Failed to inspect Binance task %s.", task_name)
                return

            if exc is not None:
                logger.error(
                    "Binance background task %s failed.",
                    task_name,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        return _callback

    async def _stop_user_data_stream(self) -> None:
        user_data_task = self._user_data_task
        self._user_data_task = None
        await self._cancel_task(user_data_task)

        keepalive_task = self._user_data_keepalive_task
        self._user_data_keepalive_task = None
        await self._cancel_task(keepalive_task)

    async def _stop_kline_stream(self, stream_name: str) -> None:
        task = self._kline_tasks.pop(stream_name, None)
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

    def _clear_keepalive_task(self, task: asyncio.Task[Any]) -> None:
        if self._user_data_keepalive_task is task:
            self._user_data_keepalive_task = None

    def _clear_kline_task(self, stream_name: str, task: asyncio.Task[Any]) -> None:
        current = self._kline_tasks.get(stream_name)
        if current is task:
            self._kline_tasks.pop(stream_name, None)
