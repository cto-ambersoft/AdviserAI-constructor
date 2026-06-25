from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.schemas.exchange_trading import NormalizedFuturesPosition


def _sort_key(row: object) -> tuple[float, str]:
    """Order fills by trade time, falling back to id — typed so mypy accepts it
    as a sort key (the raw ``traded_at or id`` lambda returns ``Any | None``)."""
    traded_at = getattr(row, "traded_at", None)
    ts = traded_at.timestamp() if isinstance(traded_at, datetime) else 0.0
    return (ts, str(getattr(row, "id", "")))


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


@dataclass(slots=True)
class RealizedBreakdown:
    """Decomposed futures PnL from a set of fills + funding.

    ``gross_realized`` is the exchange's price PnL (Binance ``realizedPnl``),
    fee-free; ``commission`` is the trading fees in quote (>= 0); ``funding`` is
    the signed funding total. ``net_realized = gross − commission + funding``;
    ``total_pnl`` adds ``unrealized`` when present.
    """

    gross_realized: float
    commission: float
    funding: float
    unrealized: float | None
    base_currency: str
    quote_currency: str

    @property
    def net_realized(self) -> float:
        return self.gross_realized - self.commission + self.funding

    @property
    def total_pnl(self) -> float:
        return self.net_realized + (self.unrealized or 0.0)


def _split_symbol(symbol: str) -> tuple[str, str]:
    if "/" not in symbol:
        return "UNKNOWN", "USDT"
    base, quote_raw = symbol.split("/", 1)
    quote = quote_raw.split(":", 1)[0]
    base_clean = base.strip().upper() or "UNKNOWN"
    quote_clean = quote.strip().upper() or "USDT"
    return base_clean, quote_clean


def _fee_to_quote(
    *,
    fee_cost: float,
    fee_currency: str | None,
    price: float,
    base: str,
    quote: str,
    mark_prices: dict[str, float] | None = None,
) -> float:
    if fee_cost <= 0 or not fee_currency:
        return 0.0
    fee_asset = fee_currency.upper()
    if fee_asset == quote:
        return float(fee_cost)
    if fee_asset == base:
        return float(fee_cost) * float(price) if price > 0 else 0.0
    # Fee paid in a third asset (e.g. BNB, the 25% discount case): value it via a
    # mark price if one was supplied, else 0 so PnL is conservative (not crash).
    if mark_prices:
        mark = mark_prices.get(fee_asset)
        if mark is not None and mark > 0:
            return float(fee_cost) * float(mark)
    return 0.0


def _fifo_pass(
    sorted_trades: list[object], base: str, quote: str
) -> tuple[float, list[_Lot], list[_Lot], float]:
    """Price-based FIFO realized PnL (net of fees) plus the resulting open lots
    and last traded price. This is the legacy engine, retained as a fallback for
    fills that lack the exchange's authoritative realized_pnl."""
    long_lots: list[_Lot] = []
    short_lots: list[_Lot] = []
    realized = 0.0
    last_price = 0.0
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
    return realized, long_lots, short_lots, last_price


def _row_commission(
    row: object, base: str, quote: str, mark_prices: dict[str, float] | None = None
) -> float:
    return _fee_to_quote(
        fee_cost=max(0.0, float(getattr(row, "fee_cost", 0.0))),
        fee_currency=getattr(row, "fee_currency", None),
        price=max(0.0, float(getattr(row, "price", 0.0))),
        base=base,
        quote=quote,
        mark_prices=mark_prices,
    )


def sum_fee_cost_quote(
    rows: Sequence[object], mark_prices: dict[str, float] | None = None
) -> float:
    """Total trading fees in quote across possibly-mixed symbols/fee currencies.

    Each fill is valued by its own symbol's base/quote: USDT fees count directly,
    base-asset fees via the fill price, third-asset fees (BNB) via ``mark_prices``
    (0 when no mark is available, so the total is conservative, never a crash).
    """
    total = 0.0
    for row in rows:
        base, quote = _split_symbol(str(getattr(row, "symbol", "")))
        total += _fee_to_quote(
            fee_cost=max(0.0, float(getattr(row, "fee_cost", 0.0))),
            fee_currency=getattr(row, "fee_currency", None),
            price=max(0.0, float(getattr(row, "price", 0.0))),
            base=base,
            quote=quote,
            mark_prices=mark_prices,
        )
    return total


def compute_realized_breakdown(
    *,
    symbol: str,
    trades: Sequence[object],
    funding: float = 0.0,
    live_position: NormalizedFuturesPosition | None = None,
    mark_prices: dict[str, float] | None = None,
) -> RealizedBreakdown:
    """Single source of truth for futures realized/unrealized from fills.

    ``gross_realized`` uses the exchange's per-fill ``realized_pnl`` when present
    (Binance) and falls back to fee-free FIFO for untagged fills. ``commission``
    is summed separately so ``net_realized = gross − commission + funding`` never
    double-counts fees. Both the per-account trades view and the per-position
    snapshot derive their numbers from here.
    """
    base, quote = _split_symbol(symbol)
    sorted_trades = sorted(trades, key=_sort_key)

    fifo_realized, long_lots, short_lots, last_price = _fifo_pass(sorted_trades, base, quote)
    commission = sum(_row_commission(row, base, quote, mark_prices) for row in sorted_trades)

    # gross realized (fee-free): exchange realized_pnl for tagged fills; for
    # untagged fills add their fees back onto the net FIFO result so gross stays
    # fee-free and ``net = gross − commission`` holds across both kinds.
    realized_values = [getattr(row, "realized_pnl", None) for row in sorted_trades]
    if any(value is not None for value in realized_values):
        gross_realized = sum(float(value) for value in realized_values if value is not None)
        untagged = [
            row
            for row, value in zip(sorted_trades, realized_values, strict=True)
            if value is None
        ]
        if untagged:
            fifo_untagged = _fifo_pass(untagged, base, quote)[0]
            commission_untagged = sum(
                _row_commission(row, base, quote, mark_prices) for row in untagged
            )
            gross_realized += fifo_untagged + commission_untagged
    else:
        gross_realized = fifo_realized + commission

    mark_price = last_price
    if (
        live_position is not None
        and live_position.mark_price is not None
        and live_position.mark_price > 0
    ):
        mark_price = float(live_position.mark_price)

    unrealized: float | None = None
    if long_lots or short_lots:
        unrealized = 0.0
        if mark_price > 0:
            for lot in long_lots:
                unrealized += (mark_price - lot.entry_price) * lot.qty
            for lot in short_lots:
                unrealized += (lot.entry_price - mark_price) * lot.qty
    if live_position is not None and live_position.unrealized_pnl is not None:
        unrealized = float(live_position.unrealized_pnl)

    return RealizedBreakdown(
        gross_realized=float(gross_realized),
        commission=float(commission),
        funding=float(funding),
        unrealized=unrealized,
        base_currency=base,
        quote_currency=quote,
    )


def calculate_futures_pnl_fifo(
    *,
    symbol: str,
    trades: Sequence[object],
    live_position: NormalizedFuturesPosition | None = None,
    mark_prices: dict[str, float] | None = None,
) -> FuturesPnlSnapshot:
    """Account/symbol-scoped realized + unrealized for the trades view.

    Thin wrapper over :func:`compute_realized_breakdown` (the single engine), so
    this view and the per-position snapshot can never diverge. ``realized`` is
    the gross exchange realized PnL (fees are reported separately as commission);
    funding is not folded in here — the trades view is funding-agnostic.
    """
    breakdown = compute_realized_breakdown(
        symbol=symbol, trades=trades, live_position=live_position, mark_prices=mark_prices
    )
    return FuturesPnlSnapshot(
        realized=breakdown.gross_realized,
        unrealized=breakdown.unrealized or 0.0,
        base_currency=breakdown.base_currency,
        quote_currency=breakdown.quote_currency,
    )
