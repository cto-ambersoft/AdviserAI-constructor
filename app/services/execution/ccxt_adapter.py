import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, SupportsFloat, cast

import ccxt.async_support as ccxt

from app.core.config import get_settings
from app.schemas.exchange_trading import (
    AttachedTriggerOrder,
    FuturesPositionSide,
    NormalizedBalance,
    NormalizedFuturesPosition,
    NormalizedOrder,
    NormalizedTrade,
    OrderSide,
    OrderStatus,
    OrderType,
    SpotPositionView,
)
from app.services.execution.base import ExchangeCredentials
from app.services.execution.errors import ExchangeServiceError


@dataclass(frozen=True, slots=True)
class _FuturesProfile:
    exchange_id: str
    default_type: str
    params: dict[str, object]
    force_isolated_margin: bool = False


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
            should_try_recover = self._looks_like_duplicate_id_error(exc.message) or exc.retryable
            if should_try_recover:
                existing = await self._find_order_by_client_order_id(
                    exchange=exchange,
                    symbol=symbol,
                    client_order_id=client_order_id,
                )
                if existing is not None:
                    return existing
            raise

    @staticmethod
    def _looks_like_duplicate_id_error(message: str) -> bool:
        lowered = message.lower()
        return (
            "duplicate" in lowered
            or "orderlinkid" in lowered
            or "clientorderid" in lowered
            or "client order id" in lowered
        )

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
                        for key in ("orderLinkId", "clientOrderId", "origClientOrderId"):
                            raw_candidate = info.get(key)
                            if isinstance(raw_candidate, str) and raw_candidate:
                                candidate = raw_candidate
                                break
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

    async def fetch_futures_trades(
        self,
        *,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[NormalizedTrade]:
        trades, _ = await self.fetch_futures_trades_page(
            symbol=symbol,
            since=since,
            limit=limit,
            cursor=None,
        )
        return trades

    async def fetch_futures_trades_page(
        self,
        *,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[NormalizedTrade], str | None]:
        profile = self._ensure_futures_supported()
        since_ms = int(since.timestamp() * 1000) if since is not None else None
        params: dict[str, object] = dict(profile.params)
        if cursor:
            if self._credentials.exchange_name == "binance":
                # Binance rejects startTime when paginating by fromId.
                since_ms = None
                params["fromId"] = cursor
            elif self._credentials.exchange_name == "bybit":
                # Keep cursor in params for exchanges that support it.
                params["cursor"] = cursor
        async with self._client(
            default_type=profile.default_type,
            exchange_id=profile.exchange_id,
        ) as exchange:
            raw = await self._call_with_retry(
                exchange.fetch_my_trades,
                symbol,
                since_ms,
                limit,
                params,
            )
        normalized = [self._normalize_trade(item) for item in raw]
        next_cursor = self._next_futures_trades_cursor(trades=normalized, previous_cursor=cursor)
        return normalized, next_cursor

    @staticmethod
    def _next_futures_trades_cursor(
        *, trades: list[NormalizedTrade], previous_cursor: str | None
    ) -> str | None:
        if not trades:
            return None
        last_trade_id = trades[-1].id
        if not last_trade_id:
            return None
        if previous_cursor is not None and last_trade_id == previous_cursor:
            return None
        return last_trade_id

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

    def _futures_profile(self) -> _FuturesProfile:
        exchange_name = self._credentials.exchange_name
        if exchange_name == "bybit":
            return _FuturesProfile(
                exchange_id="bybit",
                default_type="linear",
                params={"category": "linear"},
                force_isolated_margin=True,
            )
        if exchange_name == "binance":
            return _FuturesProfile(
                exchange_id="binanceusdm",
                default_type="swap",
                params={},
            )
        raise ExchangeServiceError(
            code="unsupported_exchange",
            message="Auto-trade futures v1 supports Bybit and Binance USDT-M only.",
        )

    def _ensure_futures_supported(self) -> _FuturesProfile:
        if self._credentials.exchange_name not in {"bybit", "binance"}:
            raise ExchangeServiceError(
                code="unsupported_exchange",
                message="Auto-trade futures v1 supports Bybit and Binance USDT-M only.",
            )
        return self._futures_profile()

    @staticmethod
    def _normalize_margin_mode(value: object) -> Literal["cross", "isolated"] | None:
        if value is None:
            return None
        raw = str(value).strip().lower()
        if raw in {"cross", "crossed"}:
            return "cross"
        if raw in {"isolated", "isolate", "isol"}:
            return "isolated"
        if raw in {"0"}:
            return "cross"
        if raw in {"1"}:
            return "isolated"
        return None

    @classmethod
    def _extract_margin_mode(
        cls, row: dict[str, Any], info: dict[str, Any] | None
    ) -> Literal["cross", "isolated"] | None:
        direct = cls._normalize_margin_mode(row.get("marginMode"))
        if direct is not None:
            return direct
        if info is None:
            return None
        for key in ("tradeMode", "marginMode"):
            parsed = cls._normalize_margin_mode(info.get(key))
            if parsed is not None:
                return parsed
        isolated = info.get("isolated")
        if isinstance(isolated, bool):
            return "isolated" if isolated else "cross"
        return None

    def _is_non_critical_bybit_margin_mode_error(self, exc: ExchangeServiceError) -> bool:
        if exc.code not in {"exchange_error", "unexpected_error"}:
            return False
        message = exc.message.lower()
        ret_code = self._extract_ret_code(exc.message)
        # Bybit may return "no state change" when already in isolated mode.
        if ret_code == 110026:
            return True
        if "margin mode" in message and ("not modified" in message or "same" in message):
            return True
        if "state change" in message:
            return True
        return False

    @staticmethod
    def _extract_binance_error_code(message: str) -> int | None:
        match = re.search(r"['\"]code['\"]\s*:\s*(-?\d+)", message)
        if match is None:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    def _is_non_critical_binance_margin_mode_error(self, exc: ExchangeServiceError) -> bool:
        if exc.code not in {"exchange_error", "unexpected_error"}:
            return False
        message = exc.message.lower()
        code = self._extract_binance_error_code(exc.message)
        if code == -4046:
            return True
        if "no need to change margin type" in message:
            return True
        return False

    async def _ensure_binance_isolated_margin(
        self,
        *,
        exchange: Any,
        symbol: str,
    ) -> None:
        if not hasattr(exchange, "set_margin_mode"):
            return
        try:
            await self._call_with_retry(exchange.set_margin_mode, "isolated", symbol, {})
        except ExchangeServiceError as exc:
            if self._is_non_critical_binance_margin_mode_error(exc):
                return
            raise

    @staticmethod
    def _is_leverage_not_modified(exc: ExchangeServiceError) -> bool:
        if exc.code not in {"exchange_error", "unexpected_error"}:
            return False
        message = exc.message.lower()
        ret_code = CcxtAdapter._extract_ret_code(exc.message)
        if ret_code == 110043:
            return True
        if "leverage" in message and "not modified" in message:
            return True
        if "no need to change leverage" in message:
            return True
        return False

    async def _ensure_bybit_isolated_margin(
        self,
        *,
        exchange: Any,
        symbol: str,
        leverage: int | None = None,
    ) -> None:
        params: dict[str, object] = {"category": "linear"}
        if leverage is not None:
            params["leverage"] = str(leverage)
        try:
            await self._call_with_retry(exchange.set_margin_mode, "isolated", symbol, params)
        except ExchangeServiceError as exc:
            if self._is_non_critical_bybit_margin_mode_error(exc):
                return
            raise

    async def set_futures_leverage(self, *, symbol: str, leverage: int) -> None:
        profile = self._ensure_futures_supported()
        try:
            async with self._client(
                default_type=profile.default_type,
                exchange_id=profile.exchange_id,
            ) as exchange:
                if profile.force_isolated_margin:
                    await self._ensure_bybit_isolated_margin(
                        exchange=exchange,
                        symbol=symbol,
                        leverage=leverage,
                    )
                if self._credentials.exchange_name == "binance":
                    await self._ensure_binance_isolated_margin(
                        exchange=exchange,
                        symbol=symbol,
                    )
                await self._call_with_retry(
                    exchange.set_leverage,
                    leverage,
                    symbol,
                    dict(profile.params),
                )
        except ExchangeServiceError as exc:
            if self._is_leverage_not_modified(exc):
                return
            raise

    @staticmethod
    def _extract_ret_code(message: str) -> int | None:
        # Bybit error payloads are often embedded into exception text.
        match = re.search(r"['\"]retCode['\"]\s*:\s*(-?\d+)", message)
        if match is None:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

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
    ) -> NormalizedOrder:
        profile = self._ensure_futures_supported()
        params: dict[str, object] = {**profile.params, "reduceOnly": bool(reduce_only)}
        if client_order_id:
            params["clientOrderId"] = client_order_id
            if self._credentials.exchange_name == "bybit":
                params["orderLinkId"] = client_order_id
            if self._credentials.exchange_name == "binance":
                params["newClientOrderId"] = client_order_id
        if self._credentials.exchange_name != "binance":
            if take_profit_price is not None:
                params["takeProfit"] = float(take_profit_price)
            if stop_loss_price is not None:
                params["stopLoss"] = float(stop_loss_price)
        if self._credentials.exchange_name == "bybit" and (
            take_profit_price is not None or stop_loss_price is not None
        ):
            # Explicit TP/SL mode makes Bybit behavior deterministic for attached triggers.
            params["tpslMode"] = "Full"
            params["tpTriggerBy"] = "MarkPrice"
            params["slTriggerBy"] = "MarkPrice"

        async with self._client(
            default_type=profile.default_type,
            exchange_id=profile.exchange_id,
        ) as exchange:
            if profile.force_isolated_margin:
                await self._ensure_bybit_isolated_margin(
                    exchange=exchange,
                    symbol=symbol,
                    leverage=None,
                )
            if self._credentials.exchange_name == "binance" and not reduce_only:
                await self._ensure_binance_isolated_margin(
                    exchange=exchange,
                    symbol=symbol,
                )
            if (
                self._credentials.exchange_name == "binance"
                and not reduce_only
                and (take_profit_price is not None or stop_loss_price is not None)
            ):
                return await self._place_binance_futures_market_with_brackets(
                    exchange=exchange,
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    client_order_id=client_order_id,
                    params=params,
                    take_profit_price=take_profit_price,
                    stop_loss_price=stop_loss_price,
                )
            raw = await self._create_order_with_idempotency(
                exchange=exchange,
                symbol=symbol,
                order_type="market",
                side=side,
                amount=amount,
                price=None,
                params=params,
                client_order_id=client_order_id,
            )
            return self._normalize_order(
                raw,
                fallback_symbol=symbol,
                fallback_side=side,
                fallback_order_type="market",
                fallback_amount=amount,
                fallback_price=None,
            )

    async def _place_binance_futures_market_with_brackets(
        self,
        *,
        exchange: Any,
        symbol: str,
        side: OrderSide,
        amount: float,
        client_order_id: str | None,
        params: dict[str, object],
        take_profit_price: float | None,
        stop_loss_price: float | None,
    ) -> NormalizedOrder:
        entry_raw = await self._create_order_with_idempotency(
            exchange=exchange,
            symbol=symbol,
            order_type="market",
            side=side,
            amount=amount,
            price=None,
            params=params,
            client_order_id=client_order_id,
        )
        hydrated_entry = await self._hydrate_created_order(
            exchange=exchange,
            symbol=symbol,
            created_order=entry_raw,
            client_order_id=client_order_id,
            expected_side=side,
            expected_order_type="market",
            expected_amount=amount,
            expected_price=None,
        )
        entry = self._normalize_order(
            hydrated_entry,
            fallback_symbol=symbol,
            fallback_side=side,
            fallback_order_type="market",
            fallback_amount=amount,
            fallback_price=None,
        )
        resolved_amount = await self._resolve_filled_amount_fast(
            exchange=exchange,
            symbol=symbol,
            entry=entry,
        )
        if resolved_amount <= 0:
            resolved_amount = amount if amount > 0 else 0.0
        if resolved_amount <= 0:
            raise ExchangeServiceError(
                code="exchange_error",
                message="Could not resolve executed quantity for Binance futures entry.",
            )

        close_side: OrderSide = "sell" if side == "buy" else "buy"
        base_client_id = client_order_id or entry.client_order_id or entry.id
        brackets: dict[str, dict[str, Any]] = {}

        if stop_loss_price is not None:
            sl_client_id = self._child_client_order_id(base_client_id, "sl")
            sl_raw = await self._create_order_with_idempotency(
                exchange=exchange,
                symbol=symbol,
                order_type="STOP_MARKET",
                side=close_side,
                amount=resolved_amount,
                price=None,
                params=self._build_binance_futures_trigger_params(
                    trigger_price=float(stop_loss_price),
                    client_order_id=sl_client_id,
                ),
                client_order_id=sl_client_id,
            )
            brackets["stop_loss"] = sl_raw

        if take_profit_price is not None:
            tp_client_id = self._child_client_order_id(base_client_id, "tp")
            tp_raw = await self._create_order_with_idempotency(
                exchange=exchange,
                symbol=symbol,
                order_type="TAKE_PROFIT_MARKET",
                side=close_side,
                amount=resolved_amount,
                price=None,
                params=self._build_binance_futures_trigger_params(
                    trigger_price=float(take_profit_price),
                    client_order_id=tp_client_id,
                ),
                client_order_id=tp_client_id,
            )
            brackets["take_profit"] = tp_raw

        if brackets:
            entry.raw["brackets"] = brackets
        return entry

    @staticmethod
    def _build_binance_futures_trigger_params(
        *,
        trigger_price: float,
        client_order_id: str | None,
    ) -> dict[str, object]:
        params: dict[str, object] = {
            "reduceOnly": True,
            "stopPrice": trigger_price,
            "workingType": "MARK_PRICE",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
            params["clientOrderId"] = client_order_id
        return params

    async def close_futures_market_reduce_only(
        self,
        *,
        symbol: str,
        side: OrderSide,
        amount: float,
        client_order_id: str | None = None,
    ) -> NormalizedOrder:
        return await self.place_futures_market_order(
            symbol=symbol,
            side=side,
            amount=amount,
            reduce_only=True,
            client_order_id=client_order_id,
            take_profit_price=None,
            stop_loss_price=None,
        )

    @staticmethod
    def _normalize_futures_side(raw: object) -> str:
        side = str(raw or "").strip().lower()
        if side in {"long", "short"}:
            return side
        if side in {"buy"}:
            return "long"
        if side in {"sell"}:
            return "short"
        return "flat"

    async def fetch_futures_position(
        self,
        *,
        symbol: str,
    ) -> NormalizedFuturesPosition | None:
        profile = self._ensure_futures_supported()
        async with self._client(
            default_type=profile.default_type,
            exchange_id=profile.exchange_id,
        ) as exchange:
            rows = await self._call_with_retry(
                exchange.fetch_positions,
                [symbol],
                dict(profile.params),
            )
        if not isinstance(rows, list):
            return None

        for row in rows:
            if not isinstance(row, dict):
                continue
            row_symbol = str(row.get("symbol") or symbol)
            if row_symbol and row_symbol != symbol:
                continue

            contracts = self._to_float(row.get("contracts"), 0.0)
            info = row.get("info")
            if contracts <= 0 and isinstance(info, dict):
                contracts = max(
                    self._to_float(info.get("size"), 0.0),
                    abs(self._to_float(info.get("positionAmt"), 0.0)),
                )
            if contracts <= 0:
                continue

            side_raw: object = row.get("side")
            if (not side_raw) and isinstance(info, dict):
                side_raw = info.get("side")
            side = self._normalize_futures_side(side_raw)
            if side == "flat":
                continue

            info_dict = info if isinstance(info, dict) else None
            take_profit_price = None
            stop_loss_price = None
            liquidation_price = None
            notional = None
            collateral = None
            if info_dict is not None:
                take_profit_price = (
                    self._to_float(info_dict.get("takeProfit"))
                    if info_dict.get("takeProfit") is not None
                    else None
                )
                stop_loss_price = (
                    self._to_float(info_dict.get("stopLoss"))
                    if info_dict.get("stopLoss") is not None
                    else None
                )
                liquidation_price = (
                    self._to_float(info_dict.get("liqPrice"))
                    if info_dict.get("liqPrice") is not None
                    else None
                )
                notional = (
                    self._to_float(info_dict.get("positionValue"))
                    if info_dict.get("positionValue") is not None
                    else None
                )
                collateral = (
                    self._to_float(info_dict.get("positionIM"))
                    if info_dict.get("positionIM") is not None
                    else None
                )
                if liquidation_price is None and info_dict.get("liquidationPrice") is not None:
                    liquidation_price = self._to_float(info_dict.get("liquidationPrice"))
                if notional is None and info_dict.get("notional") is not None:
                    notional = abs(self._to_float(info_dict.get("notional")))
                if collateral is None and info_dict.get("isolatedWallet") is not None:
                    collateral = self._to_float(info_dict.get("isolatedWallet"))
            margin_mode = self._extract_margin_mode(row, info_dict)

            return NormalizedFuturesPosition(
                symbol=row_symbol or symbol,
                side=cast(FuturesPositionSide, side),
                contracts=contracts,
                entry_price=(
                    self._to_float(row.get("entryPrice"))
                    if row.get("entryPrice") is not None
                    else None
                ),
                mark_price=(
                    self._to_float(row.get("markPrice"))
                    if row.get("markPrice") is not None
                    else None
                ),
                leverage=(
                    self._to_float(row.get("leverage")) if row.get("leverage") is not None else None
                ),
                unrealized_pnl=(
                    self._to_float(row.get("unrealizedPnl"))
                    if row.get("unrealizedPnl") is not None
                    else None
                ),
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
                liquidation_price=liquidation_price,
                margin_mode=margin_mode,
                notional=notional,
                collateral=collateral,
                raw=row,
            )
        return None

    async def fetch_ohlcv(self, *, symbol: str, timeframe: str, bars: int) -> list[list[object]]:
        async with self._client() as exchange:
            raw = await self._call_with_retry(exchange.fetch_ohlcv, symbol, timeframe, None, bars)
        return cast(list[list[object]], raw)

    @asynccontextmanager
    async def _client(
        self,
        *,
        default_type: str = "spot",
        exchange_id: str | None = None,
    ) -> AsyncIterator[Any]:
        exchange_name = exchange_id or self._credentials.exchange_name
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
            "options": {"defaultType": default_type},
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
            for key in ("orderLinkId", "clientOrderId", "origClientOrderId"):
                value = info.get(key)
                if isinstance(value, str) and value:
                    return value
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
