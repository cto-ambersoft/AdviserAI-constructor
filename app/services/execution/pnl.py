from dataclasses import dataclass
from datetime import UTC, datetime

from app.schemas.exchange_trading import NormalizedBalance, NormalizedTrade, SpotPnlAsset


@dataclass(slots=True)
class _Lot:
    qty: float
    entry_price: float


def _split_symbol(symbol: str) -> tuple[str, str] | None:
    if "/" not in symbol:
        return None
    left, right = symbol.split("/", 1)
    quote = right.split(":", 1)[0]
    base = left.strip().upper()
    quote_clean = quote.strip().upper()
    if not base or not quote_clean:
        return None
    return base, quote_clean


def calculate_spot_pnl(
    *,
    trades: list[NormalizedTrade],
    balances: list[NormalizedBalance],
    quote_asset: str,
    mark_prices: dict[str, float],
) -> tuple[list[SpotPnlAsset], float, float, float]:
    quote = quote_asset.upper()
    lots_by_asset: dict[str, list[_Lot]] = {}
    realized_by_asset: dict[str, float] = {}
    fees_by_asset: dict[str, float] = {}

    def _trade_sort_key(item: NormalizedTrade) -> datetime:
        return item.timestamp or datetime.fromtimestamp(0, tz=UTC)

    sorted_trades = sorted(trades, key=_trade_sort_key)
    for trade in sorted_trades:
        parsed = _split_symbol(trade.symbol)
        if parsed is None:
            continue
        base_asset, trade_quote = parsed
        if trade_quote != quote:
            continue
        qty = max(0.0, trade.amount)
        price = max(0.0, trade.price)
        if qty <= 0 or price <= 0:
            continue

        lots = lots_by_asset.setdefault(base_asset, [])
        fee_quote = 0.0
        if trade.fee_currency and trade.fee_currency.upper() == quote:
            fee_quote = max(0.0, trade.fee_cost)
        fees_by_asset[base_asset] = fees_by_asset.get(base_asset, 0.0) + fee_quote

        if trade.side == "buy":
            effective_cost = (price * qty) + fee_quote
            lots.append(_Lot(qty=qty, entry_price=effective_cost / qty))
            continue

        remaining_to_close = qty
        gross_realized = 0.0
        while remaining_to_close > 0 and lots:
            lot = lots[0]
            closed_qty = min(remaining_to_close, lot.qty)
            gross_realized += (price - lot.entry_price) * closed_qty
            lot.qty -= closed_qty
            remaining_to_close -= closed_qty
            if lot.qty <= 1e-12:
                lots.pop(0)

        realized_by_asset[base_asset] = (
            realized_by_asset.get(base_asset, 0.0) + gross_realized - fee_quote
        )

    balance_map = {item.asset.upper(): item.total for item in balances}
    assets = sorted(
        set(balance_map.keys()) | set(lots_by_asset.keys()) | set(realized_by_asset.keys())
    )

    rows: list[SpotPnlAsset] = []
    realized_total = 0.0
    unrealized_total = 0.0
    fees_total = 0.0
    for asset in assets:
        if asset == quote:
            continue
        qty = max(0.0, float(balance_map.get(asset, 0.0)))
        lots = lots_by_asset.get(asset, [])
        avg_entry = None
        if lots:
            lot_qty = sum(item.qty for item in lots)
            if lot_qty > 0:
                avg_entry = sum(item.qty * item.entry_price for item in lots) / lot_qty
        mark_price = mark_prices.get(asset)
        unrealized = 0.0
        if qty > 0 and avg_entry is not None and mark_price is not None and mark_price > 0:
            unrealized = (mark_price - avg_entry) * qty
        realized = realized_by_asset.get(asset, 0.0)
        fees = fees_by_asset.get(asset, 0.0)
        row = SpotPnlAsset(
            asset=asset,
            quantity=qty,
            average_entry_price=avg_entry,
            mark_price=mark_price,
            realized_pnl_quote=realized,
            unrealized_pnl_quote=unrealized,
            total_fees_quote=fees,
        )
        if qty > 0 or realized != 0 or fees != 0:
            rows.append(row)
            realized_total += realized
            unrealized_total += unrealized
            fees_total += fees
    return rows, realized_total, unrealized_total, fees_total
