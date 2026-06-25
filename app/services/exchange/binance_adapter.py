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
    PlacementWouldImmediatelyTriggerError,
    PositionAlreadyFlatError,
    PositionSide,
    PositionSnapshot,
    RateLimitState,
    TransientExchangeError,
)
from app.services.exchange.rate_limiter import AdaptiveRateLimiter

# Binance Futures REST/WS error code groups used to route ``BinanceAPIError``
# instances toward the right typed exception at the queue boundary. Kept as
# constants so the test surface and the classifier itself reference the same
# canonical set. Codes referenced from the official ``/fapi/v1/algoOrder``
# docs (Context7: ``/websites/developers_binance_derivatives``) and the
# Binance Developer Community thread on error -2022.
BINANCE_IMMEDIATE_TRIGGER_CODES: frozenset[int] = frozenset({-2021, -4131, -4046})
BINANCE_REDUCE_ONLY_CONFLICT_CODES: frozenset[int] = frozenset({-2022, -2010})
BINANCE_RATE_LIMIT_CODES: frozenset[int] = frozenset({-1003, -1015, -1016})
# -4130 — attempt to place a second GTE_GTC ``closePosition`` stop/TP in a
# direction that already has one. closePosition conditional orders are mutually
# exclusive per direction, so a replacement must cancel the old order BEFORE
# placing the new one (the reduce-only path keeps the safer place-first order).
BINANCE_CLOSE_POSITION_CONFLICT_CODES: frozenset[int] = frozenset({-4130})

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
    # Client-side keepalive for WS streams. ``ping_interval=None`` (the old
    # value) disabled keepalive entirely, so a silently-dropped connection was
    # never detected: the recv loop blocked forever, ``on_disconnect`` never
    # fired, and the stream stayed dead without reconnecting (Bug W). With a
    # ping interval the ``websockets`` lib raises ConnectionClosed on pong
    # timeout, which drives the existing reconnect path. Binance Futures WS
    # responds to client ping frames; ping_timeout is generous to avoid
    # false positives under transient latency.
    _WS_PING_INTERVAL_SECONDS = 20.0
    _WS_PING_TIMEOUT_SECONDS = 60.0

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
        """Detect whether a CCXT client is configured for demo, sandbox or mainnet.

        Authoritative signals only:

        - ``options['enableDemoTrading'] is True`` — set by
          ``ccxt_exchange.enable_demo_trading(True)``; the canonical signal
          for Binance demo trading.
        - ``isSandboxModeEnabled is True`` — set by
          ``ccxt_exchange.set_sandbox_mode(True)`` which also swaps
          ``urls['api']`` to the testnet endpoints under the hood.

        Both flags reflect the ACTIVE configuration that determines which
        endpoint outgoing requests hit. We deliberately do NOT scan
        ``urls`` for "testnet"/"demo" substrings — every fresh CCXT
        ``binance`` client ships with side-by-side ``urls['test']`` and
        ``urls['demo']`` sub-trees as a static reference. Scanning them
        produced false positives on every real-money client and rejected
        ``mode='real'`` setup with "cannot use a CCXT exchange configured
        for Binance sandbox endpoints." See regression test
        ``test_fresh_real_ccxt_binance_detects_as_mainnet``.
        """
        options = getattr(ccxt_exchange, "options", None)
        if isinstance(options, Mapping) and bool(options.get("enableDemoTrading")):
            return "demo"

        sandbox_flag = getattr(ccxt_exchange, "isSandboxModeEnabled", False)
        if isinstance(sandbox_flag, bool) and sandbox_flag:
            return "sandbox"

        return "mainnet"

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
        close_position: bool = False,
    ) -> ConditionalOrderResult:
        trigger_str = await self._price_to_precision(symbol, trigger_price)
        # ``closePosition=true`` makes Binance close the whole live position
        # at trigger time; the request must omit ``quantity`` and
        # ``reduceOnly``. Quantity rounding is skipped on that path because
        # the field is not sent.
        if close_position:
            qty_str = "0"
            qty_for_result = float(quantity) if quantity > 0 else 0.0
        else:
            qty_str = await self._amount_to_precision(symbol, quantity)
            qty_for_result = float(qty_str)

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
                close_position=close_position,
            ),
        )
        return self._conditional_result_from_payload(
            payload=payload,
            order_type="stop_loss",
            trigger_price=float(trigger_str),
            quantity=qty_for_result,
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
        close_position: bool = True,
    ) -> ConditionalOrderResult:
        """Move the SL to a new trigger price; ordering depends on SL kind.

        **reduce-only path** (``close_position=False``, used by trailing /
        breakeven / volatility): **place first, cancel last**. If the place
        call fails, the original SL remains active and the position stays
        protected. If the cancel of the old SL fails, the position transiently
        has two reduce-only SL orders — only the first to trigger executes and
        the second becomes a no-op once the position closes. Safer than
        cancel-first, which would leave a window with no SL at all.

        **closePosition path** (``close_position=True``, used by the multi-TP
        engine): **cancel first, place last**. Binance forbids two GTE_GTC
        ``closePosition`` stops in the same direction — a second one is
        rejected with -4130 — so place-first is impossible here. We cancel the
        old SL up front and accept a sub-second unprotected window. That window
        only opens while moving a closePosition SL *forward* (after a TP fill,
        with price already in profit and far from the SL), so the exposure is
        minimal. In ``closePosition`` mode Binance ignores ``new_quantity`` and
        the SL covers whatever live position remains at trigger time.
        """
        stop_side = await self._resolve_stop_loss_side(symbol)
        has_real_existing = bool(existing_order_id) and existing_order_id != "active-sl"

        # closePosition stops are mutually exclusive per direction: cancel the
        # old one BEFORE placing the replacement to avoid the -4130 rejection.
        conflict_resolved = False
        if close_position and has_real_existing:
            conflict_resolved = await self._cancel_existing_sl(symbol, existing_order_id)

        last_error: BinanceAPIError | None = None
        new_sl: ConditionalOrderResult | None = None
        # Each retry uses a unique clientAlgoId so a "duplicate id" rejection
        # at attempt N implies attempt N-1's request actually landed
        # server-side and we can recover by querying it back.
        for attempt in range(4):
            attempt_client_id = (
                client_order_id if attempt == 0 else f"{client_order_id}-r{attempt}"
            )[:36]
            try:
                new_sl = await self.place_stop_loss(
                    symbol=symbol,
                    side=stop_side,
                    quantity=new_quantity,
                    trigger_price=new_trigger_price,
                    client_order_id=attempt_client_id,
                    reduce_only=not close_position,
                    close_position=close_position,
                )
                break
            except BinanceAPIError as exc:
                last_error = exc
                # -4045 / -2022: duplicate clientAlgoId — a previous attempt
                # must have succeeded server-side before we lost the response.
                # Look the order up by the id we just sent and treat as success.
                if self._is_duplicate_client_id_error(exc):
                    recovered = await self._lookup_algo_by_client_id(
                        symbol=symbol,
                        client_order_id=attempt_client_id,
                    )
                    if recovered is not None:
                        logger.warning(
                            "cancel_and_replace_sl: recovered duplicate-id %s on %s "
                            "(attempt=%d, algoId=%s) — treating retry as success.",
                            attempt_client_id,
                            symbol,
                            attempt,
                            recovered.exchange_order_id,
                        )
                        new_sl = recovered
                        break
                # -4130: a conflicting closePosition stop is still on the book.
                # Cancel it and retry once (defence-in-depth — the up-front
                # cancel normally prevents this, but a lost cancel response or a
                # stray order can still surface it). If we have already removed
                # the order we know about and Binance still reports a conflict,
                # stop retrying and let the queue escalate.
                if (
                    close_position
                    and has_real_existing
                    and self._is_close_position_conflict_error(exc)
                ):
                    if not conflict_resolved:
                        conflict_resolved = await self._cancel_existing_sl(
                            symbol, existing_order_id
                        )
                        if conflict_resolved:
                            continue
                    break

                # Classify the error: an immediate-trigger rejection or a
                # confirmed-flat reduce-only conflict is non-retryable —
                # we want the queue to short-circuit, audit and skip rather
                # than burn through retries.
                classified = await self._classify_error(
                    exc,
                    symbol=symbol,
                    requested_trigger=new_trigger_price,
                )
                if isinstance(classified, PlacementWouldImmediatelyTriggerError):
                    raise classified from exc
                if isinstance(classified, PositionAlreadyFlatError):
                    raise classified from exc
                # ``TransientExchangeError`` from the classifier means the
                # retry loop is the right place — fall through to sleep+retry.
                if attempt == 3:
                    break
                await asyncio.sleep(0.5)

        if new_sl is None:
            # After exhausting in-adapter retries, distinguish "position is
            # gone" (queue should skip) from "we genuinely failed to place"
            # (queue should escalate). Without this, every transient
            # placement failure burned through to ``emergency_market_close``
            # which then -2022'd against the already-flat position.
            #
            # Only convert to ``PositionAlreadyFlatError`` when the position
            # is *verifiably* flat: ``get_position`` returns a snapshot with
            # size <= dust. If the lookup itself fails or returns something
            # we can't interpret, preserve the original
            # ``CriticalSLPlacementError`` so the queue's existing fatal-
            # error escalation runs (the operator still sees an audit and
            # the position is flattened defensively).
            verified_flat = False
            try:
                live = await self.get_position(symbol)
            except Exception:
                logger.exception(
                    "cancel_and_replace_sl: get_position(%s) failed during exhaust path",
                    symbol,
                )
                live = None
            else:
                if isinstance(live, PositionSnapshot):
                    try:
                        live_size = abs(float(live.size))
                    except (TypeError, ValueError):
                        live_size = None
                    if live_size is not None and live_size <= 1e-9:
                        verified_flat = True
                elif live is None:
                    verified_flat = True
            if verified_flat:
                last_payload = last_error.payload if last_error else None
                last_code: int | None = None
                if last_error is not None and isinstance(last_error.payload, Mapping):
                    raw_code = last_error.payload.get("code")
                    try:
                        last_code = int(raw_code) if raw_code is not None else None
                    except (TypeError, ValueError):
                        last_code = None
                raise PositionAlreadyFlatError(
                    "SL placement exhausted retries and exchange position is flat.",
                    code=last_code,
                    payload=last_payload,
                    symbol=symbol,
                ) from last_error
            raise CriticalSLPlacementError(
                "Unable to place replacement stop-loss after 3 retries."
            ) from last_error

        # Reduce-only path used place-first ordering, so the old SL is still
        # live — remove it now (best-effort). Failure is non-fatal: we accept a
        # brief window of two reduce-only SLs over the alternative of no SL. The
        # closePosition path already cancelled the old SL up front.
        if not close_position and has_real_existing:
            await self._cancel_existing_sl(symbol, existing_order_id)

        return new_sl

    @staticmethod
    def _is_duplicate_client_id_error(exc: BinanceAPIError) -> bool:
        """Return True if the Binance error matches a duplicate-id rejection.

        Binance Algo errors that surface for "this clientAlgoId is already in
        use" vary across endpoints — we match on common error codes and on
        the literal message fragment. False negatives are safe (the retry
        keeps running with a fresh suffix).
        """
        payload = exc.payload
        code: int | None = None
        message = ""
        if isinstance(payload, Mapping):
            try:
                code = int(payload.get("code"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                code = None
            message = str(payload.get("msg") or "")
        # -4045 / -2022 / -2010 are commonly observed on duplicate algo IDs;
        # also fall back to a substring match because Binance is inconsistent.
        if code in {-4045, -2022, -2010, -1100}:
            return True
        message_lower = message.lower()
        return "duplicate" in message_lower and "client" in message_lower

    @staticmethod
    def _is_close_position_conflict_error(exc: BinanceAPIError) -> bool:
        """Return True if Binance rejected a 2nd closePosition stop (-4130)."""
        code, message = BinanceAdapter._extract_code_and_message(exc)
        if code in BINANCE_CLOSE_POSITION_CONFLICT_CODES:
            return True
        normalized = message.lower().replace(" ", "")
        return "gte" in normalized and "closeposition" in normalized

    async def _cancel_existing_sl(self, symbol: str, existing_order_id: str) -> bool:
        """Best-effort cancel of an existing algo SL. Returns True on success."""
        try:
            await self._signed_request(
                "DELETE",
                "/fapi/v1/algoOrder",
                {
                    "symbol": self._normalize_symbol(symbol),
                    "algoId": existing_order_id,
                },
            )
            return True
        except BinanceAPIError as exc:
            logger.warning(
                "cancel_and_replace_sl: failed to cancel existing SL %s on %s: %s",
                existing_order_id,
                symbol,
                exc,
            )
            return False

    @staticmethod
    def _extract_code_and_message(exc: BinanceAPIError) -> tuple[int | None, str]:
        payload = exc.payload
        code: int | None = None
        message = ""
        if isinstance(payload, Mapping):
            try:
                code = int(payload.get("code"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                code = None
            message = str(payload.get("msg") or "")
        return code, message

    async def _classify_error(
        self,
        exc: BinanceAPIError,
        *,
        symbol: str,
        requested_trigger: float | None = None,
    ) -> Exception | None:
        """Map a ``BinanceAPIError`` to a typed retry/skip/fatal exception.

        Returns one of:
          * ``TransientExchangeError`` — caller should retry (HTTP 5xx,
            rate-limit, network instability).
          * ``PlacementWouldImmediatelyTriggerError`` — caller should skip
            with an audit; placement violates the trigger-vs-mark rule and
            re-issuing won't help.
          * ``PositionAlreadyFlatError`` — caller should skip; the live
            exchange position is gone, so a reduce-only/closePosition order
            has nothing to act on.
          * ``None`` — the error is not classifiable (treat as fatal).

        For ``-2022`` ("ReduceOnly Order is rejected") the classifier
        re-queries the live position to distinguish "position already flat"
        (skip) from "transient reduce-only conflict" (retry).
        """
        code, _ = self._extract_code_and_message(exc)
        status_code = getattr(exc, "status_code", None)

        if code in BINANCE_IMMEDIATE_TRIGGER_CODES:
            return PlacementWouldImmediatelyTriggerError(
                f"Binance rejected placement as would-immediately-trigger (code={code})",
                code=code,
                payload=exc.payload,
                requested_trigger=requested_trigger,
            )

        if code in BINANCE_RATE_LIMIT_CODES or (
            isinstance(status_code, int) and 500 <= status_code < 600
        ):
            return TransientExchangeError(
                f"Binance transient failure (status={status_code} code={code}): {exc}"
            )

        if code in BINANCE_REDUCE_ONLY_CONFLICT_CODES:
            verified_flat = False
            live_size: float | None = None
            try:
                live = await self.get_position(symbol)
            except Exception:
                logger.exception(
                    "_classify_error: get_position(%s) failed while classifying %s",
                    symbol,
                    exc,
                )
                live = None
                # Fall through: unknown live state, do not invent a typed
                # exception — let the caller treat the original
                # ``BinanceAPIError`` as fatal.
                return None
            if live is None:
                verified_flat = True
            elif isinstance(live, PositionSnapshot):
                try:
                    live_size = abs(float(live.size))
                except (TypeError, ValueError):
                    live_size = None
                if live_size is not None and live_size <= 1e-9:
                    verified_flat = True

            if verified_flat:
                return PositionAlreadyFlatError(
                    f"Binance reduce-only rejected and position is flat (code={code})",
                    code=code,
                    payload=exc.payload,
                    symbol=symbol,
                )
            # Position is alive — treat as transient so the queue's
            # retry-with-backoff loop gets a chance.
            return TransientExchangeError(
                f"Binance reduce-only rejected but live position still open "
                f"(code={code}, live_size={live_size}): {exc}"
            )

        return None

    async def _lookup_algo_by_client_id(
        self,
        symbol: str,
        client_order_id: str,
    ) -> ConditionalOrderResult | None:
        """Best-effort lookup of a previously placed algoOrder by clientAlgoId."""
        try:
            payload = await self._signed_request(
                "GET",
                "/fapi/v1/algoOrder",
                {
                    "symbol": self._normalize_symbol(symbol),
                    "clientAlgoId": client_order_id,
                },
            )
        except BinanceAPIError:
            return None
        if not isinstance(payload, Mapping):
            return None
        try:
            trigger_price = float(payload.get("triggerPrice", 0) or 0)
            quantity = float(payload.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            return None
        return ConditionalOrderResult(
            exchange_order_id=str(payload.get("algoId", "")),
            client_order_id=str(payload.get("clientAlgoId", client_order_id)),
            order_type="stop_loss",
            trigger_price=trigger_price,
            quantity=quantity,
            status=self._normalize_status(payload.get("algoStatus")),
            is_algo=True,
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
        # Closing side mirrors position direction: a LONG closes via SELL,
        # a SHORT closes via BUY. Hardcoding SELL silently broke replace_tp
        # for short positions (the new TP would have the wrong side and
        # never trigger — or worse, trigger as a re-open).
        protective_side = await self._resolve_stop_loss_side(symbol)
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
            side=protective_side,
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
        close_position: bool = False,
    ) -> dict[str, Any]:
        client_algo_id = self._client_algo_id(client_order_id)
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": self._normalize_symbol(symbol),
            "side": side.value.upper(),
            "positionSide": "BOTH",
            "type": order_type,
            "timeInForce": "GTE_GTC",
            "workingType": "CONTRACT_PRICE",
            "priceProtect": price_protect,
            "newClientAlgoId": client_algo_id,
            "clientAlgoId": client_algo_id,
        }
        if close_position:
            # Binance rejects ``closePosition=true`` combined with either
            # ``quantity`` or ``reduceOnly`` — they are mutually exclusive.
            # The order closes the entire position at trigger time, with the
            # quantity computed by the matching engine.
            params["closePosition"] = True
        else:
            params["quantity"] = self._coerce_numeric_str(quantity)
            params["reduceOnly"] = reduce_only
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
        # Binance algo orders emit two distinct lifecycle states once
        # their trigger condition is met:
        #   - ``TRIGGERED``: the conditional has fired; the underlying
        #     market order is being created.
        #   - ``FINISHED``: the underlying market order has fully filled
        #     and the algo entry can be considered done.
        # Previously both mapped to ``"triggered"``, which made the WS
        # manager process one real TP fill as TWO fill events back-to-back
        # (plus the ORDER_TRADE_UPDATE for the underlying market order =
        # three "fills" total). Combined with the matcher's price-fallback,
        # the second event would silently advance the engine onto the
        # next open TP level and cascade the position toward CLOSED. We
        # keep ``TRIGGERED`` as the canonical fill status and surface
        # ``FINISHED`` as a separate value so that ``_is_fill_event`` no
        # longer counts it as a fill — operators still get the audit log
        # entry for the lifecycle, but the engine isn't re-invoked.
        mapping = {
            "NEW": "new",
            "TRIGGERING": "new",
            "TRIGGERED": "triggered",
            "FINISHED": "finished",
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
                ping_interval=self._WS_PING_INTERVAL_SECONDS,
                ping_timeout=self._WS_PING_TIMEOUT_SECONDS,
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
                ping_interval=self._WS_PING_INTERVAL_SECONDS,
                ping_timeout=self._WS_PING_TIMEOUT_SECONDS,
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
        """Normalize a regular ``ORDER_TRADE_UPDATE`` payload (non-algo orders).

        Field-name reference (Binance USDS-M Futures user-data stream):
            i  = order id, c = client order id, s = symbol, S = side,
            o  = type (LIMIT/MARKET/STOP_MARKET/...), x = execution type,
            X  = order status, sp = stop price (trigger), p = price,
            ap = average price, L = last fill price, q = quantity,
            z  = filled cumulative qty, l = last filled qty,
            R  = reduce only, cp = close position, ps = position side.
        """
        order = payload.get("o")
        if not isinstance(order, Mapping):
            return None

        raw_type = order.get("o", order.get("ot", ""))
        raw_status = order.get("X", "")
        return {
            "type": "ORDER_TRADE_UPDATE",
            "event_type": "ORDER_TRADE_UPDATE",
            "event_time": self._to_int(payload.get("E")),
            "transaction_time": self._to_int(payload.get("T", order.get("T"))),
            "symbol": self._normalize_symbol(str(order.get("s", ""))),
            "order_id": str(order.get("i", "")),
            "client_order_id": str(order.get("c", "")),
            "status": self._normalize_status(raw_status),
            "raw_status": str(raw_status),
            "execution_type": str(order.get("x", "")),
            "order_type": self._normalize_order_type(raw_type),
            "raw_order_type": str(raw_type),
            "price": self._to_float(
                order.get("sp", order.get("p", order.get("ap"))),
                default=0.0,
            ),
            "average_price": self._to_float(order.get("ap"), default=0.0),
            "last_fill_price": self._to_float(order.get("L"), default=0.0),
            "trigger_price": self._to_float(order.get("sp"), default=0.0),
            "quantity": self._to_float(order.get("q"), default=0.0),
            "filled_quantity": self._to_float(order.get("z"), default=0.0),
            "last_filled_quantity": self._to_float(order.get("l"), default=0.0),
            "side": str(order.get("S", "")).lower(),
            "position_side": str(order.get("ps", "")).lower(),
            "reduce_only": bool(order.get("R", False)),
            "close_position": bool(order.get("cp", False)),
            "is_algo": False,
            "raw": dict(order),
        }

    def _normalize_algo_update(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Normalize an ``ALGO_UPDATE`` payload (conditional / TP-SL / trailing).

        Field-name reference (Binance USDS-M Futures Event Algo Order Update):
            aid = algo id, caid = client algo id, at = algo type,
            o   = order type (TAKE_PROFIT / STOP_MARKET / TRAILING_STOP_MARKET),
            s   = symbol, S = side, ps = position side, f = time in force,
            q   = quantity, X = algo status,
            ai  = matched order id (after trigger), ap = average fill price,
            aq  = executed quantity, act = actual order type post-trigger,
            tp  = trigger price, p = order price, rm = failure reason.

        The previous shared normalizer used ``ORDER_TRADE_UPDATE`` field names
        (``i``/``c``/``sp``/``z``) which silently produced empty ``order_id``,
        ``client_order_id``, and zero ``trigger_price`` / ``filled_quantity``
        — making ``_match_tp_level`` impossible and the SL move never fired.
        """
        # Doc canonical key is ``o`` (nested object). ``ao`` retained for any
        # legacy payload variants observed in the wild.
        order_payload: Mapping[str, Any] | None = None
        for key in ("o", "ao"):
            candidate = payload.get(key)
            if isinstance(candidate, Mapping):
                order_payload = candidate
                break
        if order_payload is None:
            return None

        raw_type = order_payload.get("o", "")
        raw_status = order_payload.get("X", "")
        # ``act`` is the order type the matching engine actually executed on
        # trigger (typically MARKET). Fall back to the algo's own type so the
        # downstream order_type classifier still routes the event as TP/SL.
        execution_type = str(order_payload.get("act", "")).upper()
        trigger_price = self._to_float(order_payload.get("tp"), default=0.0)
        order_price = self._to_float(order_payload.get("p"), default=0.0)
        average_price = self._to_float(order_payload.get("ap"), default=0.0)

        return {
            "type": "ALGO_UPDATE",
            "event_type": "ALGO_UPDATE",
            "event_time": self._to_int(payload.get("E")),
            "transaction_time": self._to_int(payload.get("T")),
            "symbol": self._normalize_symbol(str(order_payload.get("s", ""))),
            # algo id (cancel/lookup uses this), echoed by REST as ``algoId``.
            "order_id": str(order_payload.get("aid", "")),
            "client_order_id": str(order_payload.get("caid", "")),
            "status": self._normalize_status(raw_status),
            "raw_status": str(raw_status),
            "execution_type": execution_type,
            "order_type": self._normalize_order_type(raw_type),
            "raw_order_type": str(raw_type),
            "actual_order_type": execution_type or None,
            # ``ai`` is the matched (regular) order id created when the algo
            # triggers; surface it so the WS layer can correlate the follow-up
            # ORDER_TRADE_UPDATE for the actual MARKET fill.
            "matched_order_id": str(order_payload.get("ai", "")),
            "price": (trigger_price or order_price or average_price),
            "average_price": average_price,
            "last_fill_price": average_price,
            "trigger_price": trigger_price,
            "quantity": self._to_float(order_payload.get("q"), default=0.0),
            "filled_quantity": self._to_float(order_payload.get("aq"), default=0.0),
            "last_filled_quantity": self._to_float(order_payload.get("aq"), default=0.0),
            "side": str(order_payload.get("S", "")).lower(),
            "position_side": str(order_payload.get("ps", "")).lower(),
            "reduce_only": True,  # algo conditional orders are always reduce-only-equivalent
            "close_position": False,
            "algo_type": str(order_payload.get("at", "")).upper(),
            "failure_reason": str(order_payload.get("rm", "")) or None,
            "is_algo": True,
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
