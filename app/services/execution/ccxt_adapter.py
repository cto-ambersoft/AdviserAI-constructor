import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, SupportsFloat, cast

import ccxt.async_support as ccxt

from app.core.config import get_settings
from app.schemas.exchange_trading import (
    AttachedTriggerOrder,
    NormalizedBalance,
    NormalizedOrder,
    NormalizedTrade,
    OrderSide,
    OrderStatus,
    OrderType,
    SpotPositionView,
)
from app.services.execution.base import ExchangeCredentials
from app.services.execution.errors import ExchangeServiceError


class CcxtAdapter:
    def __init__(self, credentials: ExchangeCredentials) -> None:
        self._credentials = credentials
        self._settings = get_settings()

    @staticmethod
    def _to_datetime(timestamp_ms: int | None) -> datetime | None:
        if timestamp_ms is None:
            return None
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)

    @staticmethod
    def _to_float(value: object, default: float = 0.0) -> float:
        if not isinstance(value, (str, bytes, bytearray, int, float)):
            return default
        try:
            return float(cast(SupportsFloat | str | bytes | bytearray, value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_order_status(value: object) -> OrderStatus:
        raw = str(value or "").lower()
        if raw in {"open", "closed", "canceled", "rejected", "expired"}:
            return cast(OrderStatus, raw)
        if raw in {"cancelled"}:
            return "canceled"
        return "unknown"

    async def ping(self) -> None:
        async with self._client() as exchange:
            await self._call_with_retry(exchange.load_markets)

    async def fetch_balance(self) -> list[NormalizedBalance]:
        async with self._client() as exchange:
            raw = await self._call_with_retry(exchange.fetch_balance)

        total = raw.get("total", {})
        free = raw.get("free", {})
        used = raw.get("used", {})
        if not isinstance(total, dict) or not isinstance(free, dict) or not isinstance(used, dict):
            return []

        assets = sorted(set(total.keys()) | set(free.keys()) | set(used.keys()))
        balances: list[NormalizedBalance] = []
        for asset in assets:
            if not isinstance(asset, str):
                continue
            row = NormalizedBalance(
                asset=asset,
                free=self._to_float(free.get(asset, 0.0)),
                used=self._to_float(used.get(asset, 0.0)),
                total=self._to_float(total.get(asset, 0.0)),
            )
            if row.total > 0 or row.free > 0 or row.used > 0:
                balances.append(row)
        return balances

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
    ) -> NormalizedOrder:
        async with self._client() as exchange:
            if (
                self._credentials.exchange_name == "bybit"
                and order_type == "market"
                and (attached_take_profit is not None or attached_stop_loss is not None)
            ):
                return await self._place_bybit_market_with_brackets(
                    exchange=exchange,
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    client_order_id=client_order_id,
                    attached_take_profit=attached_take_profit,
                    attached_stop_loss=attached_stop_loss,
                )

            params: dict[str, object] = {}
            if client_order_id:
                params["clientOrderId"] = client_order_id
                params["orderLinkId"] = client_order_id
            if attached_take_profit is not None:
                params["takeProfit"] = self._build_attached_order_payload(attached_take_profit)
            if attached_stop_loss is not None:
                params["stopLoss"] = self._build_attached_order_payload(attached_stop_loss)

            raw = await self._create_order_with_idempotency(
                exchange=exchange,
                symbol=symbol,
                order_type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=params,
                client_order_id=client_order_id,
            )
            return self._normalize_order(
                raw,
                fallback_symbol=symbol,
                fallback_side=side,
                fallback_order_type=order_type,
                fallback_amount=amount,
                fallback_price=price,
            )

    @staticmethod
    def _build_attached_order_payload(attached: AttachedTriggerOrder) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": attached.order_type,
            "triggerPrice": attached.trigger_price,
        }
        if attached.order_type == "limit" and attached.price is not None:
            payload["price"] = attached.price
        return payload

    async def _place_bybit_market_with_brackets(
        self,
        *,
        exchange: Any,
        symbol: str,
        side: OrderSide,
        amount: float,
        client_order_id: str | None,
        attached_take_profit: AttachedTriggerOrder | None,
        attached_stop_loss: AttachedTriggerOrder | None,
    ) -> NormalizedOrder:
        entry_raw = await self._create_order_with_idempotency(
            exchange=exchange,
            symbol=symbol,
            order_type="market",
            side=side,
            amount=amount,
            price=None,
            params=self._build_client_id_params(client_order_id),
            client_order_id=client_order_id,
        )
        entry = self._normalize_order(
            entry_raw,
            fallback_symbol=symbol,
            fallback_side=side,
            fallback_order_type="market",
            fallback_amount=amount,
            fallback_price=None,
        )
        filled_qty = await self._resolve_filled_amount_fast(
            exchange=exchange, symbol=symbol, entry=entry
        )
        if filled_qty <= 0:
            raise ExchangeServiceError(
                code="exchange_error",
                message="Could not resolve executed quantity for market order.",
            )

        close_side: OrderSide = "sell" if side == "buy" else "buy"
        base_client_id = client_order_id or entry.client_order_id or entry.id
        brackets: dict[str, dict[str, Any]] = {}

        if attached_take_profit is not None:
            tp_client_id = self._child_client_order_id(base_client_id, "tp")
            tp_raw = await self._create_independent_trigger_order(
                exchange=exchange,
                symbol=symbol,
                side=close_side,
                amount=filled_qty,
                trigger=attached_take_profit,
                trigger_kind="takeProfitPrice",
                client_order_id=tp_client_id,
            )
            brackets["take_profit"] = tp_raw

        if attached_stop_loss is not None:
            sl_client_id = self._child_client_order_id(base_client_id, "sl")
            sl_raw = await self._create_independent_trigger_order(
                exchange=exchange,
                symbol=symbol,
                side=close_side,
                amount=filled_qty,
                trigger=attached_stop_loss,
                trigger_kind="stopLossPrice",
                client_order_id=sl_client_id,
            )
            brackets["stop_loss"] = sl_raw

        if brackets:
            entry.raw["brackets"] = brackets
        return entry

    @staticmethod
    def _build_client_id_params(client_order_id: str | None) -> dict[str, object]:
        if not client_order_id:
            return {}
        return {"clientOrderId": client_order_id, "orderLinkId": client_order_id}

    @staticmethod
    def _child_client_order_id(base_client_id: str | None, suffix: str) -> str | None:
        if not base_client_id:
            return None
        max_len = 64
        affix = f"-{suffix}"
        trimmed = base_client_id[: max_len - len(affix)]
        return f"{trimmed}{affix}"

    async def _create_independent_trigger_order(
        self,
        *,
        exchange: Any,
        symbol: str,
        side: OrderSide,
        amount: float,
        trigger: AttachedTriggerOrder,
        trigger_kind: str,
        client_order_id: str | None,
    ) -> dict[str, Any]:
        params = self._build_client_id_params(client_order_id)
        params[trigger_kind] = trigger.trigger_price
        return await self._create_order_with_idempotency(
            exchange=exchange,
            symbol=symbol,
            order_type=trigger.order_type,
            side=side,
            amount=amount,
            price=trigger.price,
            params=params,
            client_order_id=client_order_id,
        )

    async def _create_order_with_idempotency(
        self,
        *,
        exchange: Any,
        symbol: str,
        order_type: OrderType,
        side: OrderSide,
        amount: float,
        price: float | None,
        params: dict[str, object],
        client_order_id: str | None,
    ) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                await self._call_with_retry(
                    exchange.create_order,
                    symbol,
                    order_type,
                    side,
                    amount,
                    price,
                    params,
                ),
            )
        except ExchangeServiceError as exc:
            if not client_order_id:
                raise
            if not self._looks_like_duplicate_id_error(exc.message):
                raise
            existing = await self._find_order_by_client_order_id(
                exchange=exchange,
                symbol=symbol,
                client_order_id=client_order_id,
            )
            if existing is None:
                raise
            return existing

    @staticmethod
    def _looks_like_duplicate_id_error(message: str) -> bool:
        lowered = message.lower()
        return "duplicate" in lowered or "orderlinkid" in lowered or "clientorderid" in lowered

    async def _find_order_by_client_order_id(
        self,
        *,
        exchange: Any,
        symbol: str,
        client_order_id: str,
    ) -> dict[str, Any] | None:
        for fetcher in (exchange.fetch_open_orders, exchange.fetch_closed_orders):
            try:
                rows = await self._call_with_retry(fetcher, symbol, None, 200)
            except ExchangeServiceError:
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                candidate = row.get("clientOrderId")
                if not isinstance(candidate, str):
                    info = row.get("info")
                    if isinstance(info, dict):
                        raw_candidate = info.get("orderLinkId")
                        if isinstance(raw_candidate, str):
                            candidate = raw_candidate
                if candidate == client_order_id:
                    return row
        return None

    async def _resolve_filled_amount_fast(
        self,
        *,
        exchange: Any,
        symbol: str,
        entry: NormalizedOrder,
    ) -> float:
        executed = max(entry.filled, max(entry.amount - entry.remaining, 0.0))
        if executed > 0:
            return executed

        fallback_amount = entry.amount if entry.amount > 0 else 0.0

        try:
            trades = await self._call_with_retry(exchange.fetch_my_trades, symbol, None, 100)
        except ExchangeServiceError:
            return fallback_amount

        filled_from_trades = 0.0
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            if str(trade.get("order")) != entry.id:
                continue
            filled_from_trades += self._to_float(trade.get("amount"), 0.0)
        if filled_from_trades > 0:
            return filled_from_trades
        return fallback_amount

    async def fetch_order_detail(self, *, order_id: str, symbol: str) -> NormalizedOrder:
        async with self._client() as exchange:
            raw = await self._find_order_by_id(exchange=exchange, symbol=symbol, order_id=order_id)
            if raw is None and hasattr(exchange, "fetch_order"):
                try:
                    fetched = await self._call_with_retry(exchange.fetch_order, order_id, symbol)
                except ExchangeServiceError:
                    fetched = None
                if isinstance(fetched, dict):
                    raw = fetched
            if raw is None:
                raise ExchangeServiceError(code="not_found", message="Order not found.")
            hydrated = await self._hydrate_created_order(
                exchange=exchange,
                symbol=symbol,
                created_order=raw,
                client_order_id=self._extract_client_order_id(raw),
                expected_side="buy",
                expected_order_type="limit",
                expected_amount=0.0,
                expected_price=None,
            )
            return self._normalize_order(hydrated, fallback_symbol=symbol)

    async def _find_order_by_id(
        self, *, exchange: Any, symbol: str, order_id: str
    ) -> dict[str, Any] | None:
        for fetcher in (exchange.fetch_open_orders, exchange.fetch_closed_orders):
            try:
                rows = await self._call_with_retry(fetcher, symbol, None, 200)
            except ExchangeServiceError:
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("id")) == order_id:
                    return row
        return None

    async def _hydrate_created_order(
        self,
        *,
        exchange: Any,
        symbol: str,
        created_order: dict[str, Any],
        client_order_id: str | None,
        expected_side: OrderSide,
        expected_order_type: OrderType,
        expected_amount: float,
        expected_price: float | None,
    ) -> dict[str, Any]:
        created_id = str(created_order.get("id", ""))
        best: dict[str, Any] = dict(created_order)

        for _ in range(4):
            candidate: dict[str, Any] | None = None
            if created_id:
                candidate = await self._find_order_by_id(
                    exchange=exchange,
                    symbol=symbol,
                    order_id=created_id,
                )
            if candidate is None and client_order_id:
                candidate = await self._find_order_by_client_order_id(
                    exchange=exchange,
                    symbol=symbol,
                    client_order_id=client_order_id,
                )
            if candidate is not None:
                best = self._merge_order_payload(best, candidate)
                if self._has_meaningful_order_fields(best):
                    break
            await asyncio.sleep(0.25)

        trades = await self._fetch_order_trades(
            exchange=exchange, symbol=symbol, order_id=created_id
        )
        best = self._enrich_order_from_trades(best, trades)
        best = self._fill_order_fallbacks(
            best,
            symbol=symbol,
            side=expected_side,
            order_type=expected_order_type,
            amount=expected_amount,
            price=expected_price,
        )
        return best

    async def _fetch_order_trades(
        self,
        *,
        exchange: Any,
        symbol: str,
        order_id: str,
    ) -> list[dict[str, Any]]:
        if not order_id:
            return []
        try:
            rows = await self._call_with_retry(exchange.fetch_my_trades, symbol, None, 200)
        except ExchangeServiceError:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("order")) == order_id:
                out.append(row)
        return out

    def _enrich_order_from_trades(
        self,
        order_payload: dict[str, Any],
        trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not trades:
            return order_payload
        out = dict(order_payload)
        filled = self._to_float(out.get("filled"), 0.0)
        amount = self._to_float(out.get("amount"), 0.0)
        cost = self._to_float(out.get("cost"), 0.0)
        if filled <= 0:
            filled = sum(self._to_float(item.get("amount"), 0.0) for item in trades)
            out["filled"] = filled
        if cost <= 0:
            cost = sum(self._to_float(item.get("cost"), 0.0) for item in trades)
            if cost > 0:
                out["cost"] = cost
        if out.get("average") is None and filled > 0 and cost > 0:
            out["average"] = cost / filled
        if amount <= 0 and filled > 0:
            out["amount"] = filled
            amount = filled
        if out.get("remaining") is None and amount > 0 and filled >= 0:
            out["remaining"] = max(amount - filled, 0.0)
        if out.get("timestamp") is None:
            timestamps: list[int] = []
            for item in trades:
                raw_ts = item.get("timestamp")
                if isinstance(raw_ts, int) and raw_ts > 0:
                    timestamps.append(raw_ts)
            if timestamps:
                out["timestamp"] = min(timestamps)
        if out.get("status") is None and filled > 0:
            out["status"] = "closed" if self._to_float(out.get("remaining"), 0.0) <= 0 else "open"
        return out

    def _fill_order_fallbacks(
        self,
        payload: dict[str, Any],
        *,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: float,
        price: float | None,
    ) -> dict[str, Any]:
        out = dict(payload)
        if not isinstance(out.get("symbol"), str) or not str(out.get("symbol")):
            out["symbol"] = symbol
        if out.get("side") is None:
            out["side"] = side
        if out.get("type") is None:
            out["type"] = order_type
        if out.get("amount") is None:
            out["amount"] = amount
        if out.get("price") is None and price is not None:
            out["price"] = price
        if out.get("filled") is None:
            out["filled"] = 0.0
        if out.get("remaining") is None:
            resolved_amount = self._to_float(out.get("amount"), amount)
            resolved_filled = self._to_float(out.get("filled"), 0.0)
            out["remaining"] = max(resolved_amount - resolved_filled, 0.0)
        if out.get("status") is None:
            out["status"] = "open"
        return out

    @staticmethod
    def _merge_order_payload(base: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
        out = dict(base)
        for key, value in fresh.items():
            if value is None:
                continue
            if key == "info":
                base_info = out.get("info")
                if isinstance(base_info, dict) and isinstance(value, dict):
                    merged_info = dict(base_info)
                    for info_key, info_value in value.items():
                        if info_value is not None:
                            merged_info[info_key] = info_value
                    out["info"] = merged_info
                else:
                    out["info"] = value
                continue
            out[key] = value
        return out

    def _has_meaningful_order_fields(self, payload: dict[str, Any]) -> bool:
        has_side = isinstance(payload.get("side"), str) and bool(payload.get("side"))
        has_type = isinstance(payload.get("type"), str) and bool(payload.get("type"))
        has_amount = payload.get("amount") is not None
        has_status = payload.get("status") is not None
        return has_side and has_type and has_amount and has_status

    async def cancel_order(self, *, order_id: str, symbol: str | None = None) -> NormalizedOrder:
        async with self._client() as exchange:
            raw = await self._call_with_retry(exchange.cancel_order, order_id, symbol)
        return self._normalize_order(raw)

    async def fetch_open_orders(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[NormalizedOrder]:
        async with self._client() as exchange:
            raw = await self._call_with_retry(exchange.fetch_open_orders, symbol, None, limit)
        return [self._normalize_order(item) for item in raw]

    async def fetch_closed_orders(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[NormalizedOrder]:
        async with self._client() as exchange:
            raw = await self._call_with_retry(exchange.fetch_closed_orders, symbol, None, limit)
        return [self._normalize_order(item) for item in raw]

    async def fetch_trades(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[NormalizedTrade]:
        async with self._client() as exchange:
            raw = await self._call_with_retry(exchange.fetch_my_trades, symbol, None, limit)
        return [self._normalize_trade(item) for item in raw]

    async def fetch_spot_positions_view(
        self,
        *,
        quote_asset: str = "USDT",
    ) -> list[SpotPositionView]:
        balances = await self.fetch_balance()
        rows: list[SpotPositionView] = []
        async with self._client() as exchange:
            for balance in balances:
                if balance.total <= 0:
                    continue
                if balance.asset.upper() == quote_asset.upper():
                    rows.append(
                        SpotPositionView(
                            asset=balance.asset,
                            quantity=balance.total,
                            mark_price=1.0,
                            market_value_quote=balance.total,
                            unrealized_pnl_quote=0.0,
                        )
                    )
                    continue

                symbol = f"{balance.asset}/{quote_asset}"
                try:
                    ticker = await self._call_with_retry(exchange.fetch_ticker, symbol)
                except ExchangeServiceError:
                    rows.append(SpotPositionView(asset=balance.asset, quantity=balance.total))
                    continue
                mark_price = self._to_float(ticker.get("last"), 0.0)
                if mark_price <= 0:
                    rows.append(SpotPositionView(asset=balance.asset, quantity=balance.total))
                    continue
                rows.append(
                    SpotPositionView(
                        asset=balance.asset,
                        quantity=balance.total,
                        mark_price=mark_price,
                        market_value_quote=balance.total * mark_price,
                    )
                )
        return rows

    async def fetch_ohlcv(self, *, symbol: str, timeframe: str, bars: int) -> list[list[object]]:
        async with self._client() as exchange:
            raw = await self._call_with_retry(exchange.fetch_ohlcv, symbol, timeframe, None, bars)
        return cast(list[list[object]], raw)

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        exchange_name = self._credentials.exchange_name
        exchange_cls = getattr(ccxt, exchange_name, None)
        if exchange_cls is None:
            raise ExchangeServiceError(
                code="unsupported_exchange",
                message=f"Exchange '{exchange_name}' is not supported.",
            )

        config: dict[str, object] = {
            "apiKey": self._credentials.api_key,
            "secret": self._credentials.api_secret,
            "enableRateLimit": True,
            "timeout": self._settings.exchange_http_timeout_seconds * 1000,
            "options": {"defaultType": "spot"},
        }
        if self._credentials.passphrase:
            config["password"] = self._credentials.passphrase
        exchange = exchange_cls(config)
        if self._credentials.mode == "demo":
            self._configure_demo_mode(exchange)
        try:
            await self._call_with_retry(exchange.load_markets)
            yield exchange
        except Exception as exc:
            if isinstance(exc, ExchangeServiceError):
                raise
            raise self._map_ccxt_error(exc) from exc
        finally:
            await exchange.close()

    @staticmethod
    def _configure_demo_mode(exchange: Any) -> None:
        # Bybit has dedicated demo trading domains, which differ from plain testnet.
        # Prefer CCXT demo mode when available; fallback to sandbox for exchanges without it.
        try:
            exchange.enable_demo_trading(True)
            return
        except Exception:
            pass
        try:
            exchange.set_sandbox_mode(True)
        except Exception:
            # Some exchanges do not support either mode switch in CCXT.
            return

    async def _call_with_retry(
        self,
        call: Callable[..., Awaitable[Any]],
        *args: object,
    ) -> Any:
        attempts = max(1, self._settings.exchange_max_retries)
        delay_seconds = max(self._settings.exchange_retry_delay_ms, 0) / 1000.0
        last_error: ExchangeServiceError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await call(*args)
            except Exception as exc:
                mapped = self._map_ccxt_error(exc)
                if not mapped.retryable or attempt >= attempts:
                    raise mapped from exc
                last_error = mapped
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds * attempt)
        if last_error is not None:
            raise last_error
        raise ExchangeServiceError(code="unknown_error", message="Unknown exchange error.")

    @staticmethod
    def _map_ccxt_error(exc: Exception) -> ExchangeServiceError:
        if isinstance(exc, (ccxt.AuthenticationError, ccxt.PermissionDenied)):
            return ExchangeServiceError(code="authentication_failed", message=str(exc))
        if isinstance(exc, ccxt.BadSymbol):
            return ExchangeServiceError(code="invalid_symbol", message=str(exc))
        if isinstance(exc, ccxt.InsufficientFunds):
            return ExchangeServiceError(code="insufficient_funds", message=str(exc))
        if isinstance(exc, ccxt.RateLimitExceeded):
            return ExchangeServiceError(code="rate_limited", message=str(exc), retryable=True)
        if isinstance(exc, (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable)):
            return ExchangeServiceError(
                code="temporary_unavailable", message=str(exc), retryable=True
            )
        if isinstance(exc, ccxt.ExchangeError):
            return ExchangeServiceError(code="exchange_error", message=str(exc))
        return ExchangeServiceError(code="unexpected_error", message=str(exc))

    def _normalize_order(
        self,
        payload: dict[str, Any],
        *,
        fallback_symbol: str | None = None,
        fallback_side: OrderSide | None = None,
        fallback_order_type: OrderType | None = None,
        fallback_amount: float | None = None,
        fallback_price: float | None = None,
    ) -> NormalizedOrder:
        side = str(payload.get("side", fallback_side or "")).lower()
        if side not in {"buy", "sell"}:
            side = fallback_side or "buy"
        order_type = str(payload.get("type", fallback_order_type or "")).lower()
        if order_type not in {"market", "limit"}:
            order_type = fallback_order_type or "limit"
        amount = self._to_float(payload.get("amount"), fallback_amount or 0.0)
        filled = self._to_float(payload.get("filled"), 0.0)
        remaining_default = max(amount - filled, 0.0)
        remaining = self._to_float(payload.get("remaining"), remaining_default)
        return NormalizedOrder(
            id=str(payload.get("id", "")),
            client_order_id=self._extract_client_order_id(payload),
            symbol=str(payload.get("symbol", fallback_symbol or "")),
            side=cast(OrderSide, side),
            order_type=cast(OrderType, order_type),
            status=self._normalize_order_status(payload.get("status")),
            amount=amount,
            filled=filled,
            remaining=remaining,
            price=(
                self._to_float(payload.get("price"))
                if payload.get("price") is not None
                else fallback_price
            ),
            average=(
                self._to_float(payload.get("average"))
                if payload.get("average") is not None
                else None
            ),
            cost=self._to_float(payload.get("cost")) if payload.get("cost") is not None else None,
            timestamp=self._to_datetime(
                payload.get("timestamp") if isinstance(payload.get("timestamp"), int) else None
            ),
            raw=payload,
        )

    @staticmethod
    def _extract_client_order_id(payload: dict[str, Any]) -> str | None:
        direct = payload.get("clientOrderId")
        if isinstance(direct, str) and direct:
            return direct
        info = payload.get("info")
        if isinstance(info, dict):
            order_link_id = info.get("orderLinkId")
            if isinstance(order_link_id, str) and order_link_id:
                return order_link_id
        return None

    def _normalize_trade(self, payload: dict[str, Any]) -> NormalizedTrade:
        side = str(payload.get("side", "")).lower()
        if side not in {"buy", "sell"}:
            side = "buy"
        fee = payload.get("fee", {})
        fee_cost = 0.0
        fee_currency: str | None = None
        if isinstance(fee, dict):
            fee_cost = self._to_float(fee.get("cost", 0.0))
            raw_currency = fee.get("currency")
            if isinstance(raw_currency, str):
                fee_currency = raw_currency

        return NormalizedTrade(
            id=str(payload.get("id", "")),
            order_id=str(payload.get("order")) if payload.get("order") is not None else None,
            symbol=str(payload.get("symbol", "")),
            side=cast(OrderSide, side),
            amount=self._to_float(payload.get("amount")),
            price=self._to_float(payload.get("price")),
            cost=self._to_float(payload.get("cost")) if payload.get("cost") is not None else None,
            fee_cost=fee_cost,
            fee_currency=fee_currency,
            timestamp=self._to_datetime(
                payload.get("timestamp") if isinstance(payload.get("timestamp"), int) else None
            ),
            raw=payload,
        )
