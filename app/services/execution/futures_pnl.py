from dataclasses import dataclass

from app.schemas.exchange_trading import NormalizedFuturesPosition


@dataclass(slots=True)
class _Lot:
    qty: float
    entry_price: float


@dataclass(slots=True)
class FuturesPnlSnapshot:
    realized: float
    unrealized: float
    base_currency: str
    quote_currency: str


def _split_symbol(symbol: str) -> tuple[str, str]:
    if "/" not in symbol:
        return "UNKNOWN", "USDT"
    base, quote_raw = symbol.split("/", 1)
    quote = quote_raw.split(":", 1)[0]
    base_clean = base.strip().upper() or "UNKNOWN"
    quote_clean = quote.strip().upper() or "USDT"
    return base_clean, quote_clean


def _fee_to_quote(*, fee_cost: float, fee_currency: str | None, price: float, base: str, quote: str) -> float:
    if fee_cost <= 0 or not fee_currency:
        return 0.0
    fee_asset = fee_currency.upper()
    if fee_asset == quote:
        return float(fee_cost)
    if fee_asset == base:
        return float(fee_cost) * float(price) if price > 0 else 0.0
    return 0.0


def calculate_futures_pnl_fifo(
    *,
    symbol: str,
    trades: list[object],
    live_position: NormalizedFuturesPosition | None = None,
) -> FuturesPnlSnapshot:
    base, quote = _split_symbol(symbol)
    long_lots: list[_Lot] = []
    short_lots: list[_Lot] = []
    realized = 0.0
    last_price = 0.0

    sorted_trades = sorted(
        trades,
        key=lambda row: getattr(row, "traded_at", None) or getattr(row, "id", 0),
    )
    for row in sorted_trades:
        side = str(getattr(row, "side", "")).lower()
        qty = max(0.0, float(getattr(row, "amount", 0.0)))
        price = max(0.0, float(getattr(row, "price", 0.0)))
        fee_cost = max(0.0, float(getattr(row, "fee_cost", 0.0)))
        fee_currency = getattr(row, "fee_currency", None)
        if qty <= 0 or price <= 0 or side not in {"buy", "sell"}:
            continue
        last_price = price
        fee_quote = _fee_to_quote(
            fee_cost=fee_cost,
            fee_currency=fee_currency,
            price=price,
            base=base,
            quote=quote,
        )
        realized -= fee_quote
        remaining = qty
        if side == "buy":
            while remaining > 0 and short_lots:
                lot = short_lots[0]
                close_qty = min(remaining, lot.qty)
                realized += (lot.entry_price - price) * close_qty
                lot.qty -= close_qty
                remaining -= close_qty
                if lot.qty <= 1e-12:
                    short_lots.pop(0)
            if remaining > 0:
                long_lots.append(_Lot(qty=remaining, entry_price=price))
            continue

        while remaining > 0 and long_lots:
            lot = long_lots[0]
            close_qty = min(remaining, lot.qty)
            realized += (price - lot.entry_price) * close_qty
            lot.qty -= close_qty
            remaining -= close_qty
            if lot.qty <= 1e-12:
                long_lots.pop(0)
        if remaining > 0:
            short_lots.append(_Lot(qty=remaining, entry_price=price))

    mark_price = last_price
    if live_position is not None and live_position.mark_price is not None and live_position.mark_price > 0:
        mark_price = float(live_position.mark_price)

    unrealized = 0.0
    if mark_price > 0:
        for lot in long_lots:
            unrealized += (mark_price - lot.entry_price) * lot.qty
        for lot in short_lots:
            unrealized += (lot.entry_price - mark_price) * lot.qty

    if live_position is not None and live_position.unrealized_pnl is not None:
        unrealized = float(live_position.unrealized_pnl)

    return FuturesPnlSnapshot(
        realized=float(realized),
        unrealized=float(unrealized),
        base_currency=base,
        quote_currency=quote,
    )
