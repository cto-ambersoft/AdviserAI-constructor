"""W9 PnL-accuracy invariant matrix.

One place that pins the audit's invariants on the shared futures engine
(``compute_realized_breakdown`` + helpers). Pure-function level so it stays fast
and unambiguous. DB-integration scenarios (OPEN partial closes, multi-TP close,
daily-loss net, idempotent sync) live in their feature tests:
  * tests/test_auto_trade_service.py — position snapshot OPEN/CLOSED + daily-loss
  * tests/test_income_sync.py — funding sync + sum_funding
  * tests/test_ledger_realized_pnl.py — realized_pnl extraction + backfill

Canonical decomposition under test: net = gross_realized − commission + funding.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.execution.futures_pnl import (
    calculate_futures_pnl_fifo,
    compute_realized_breakdown,
    sum_fee_cost_quote,
)

SYMBOL = "BTC/USDT:USDT"


def _fill(
    *,
    side: str,
    price: float,
    amount: float = 1.0,
    fee_cost: float = 0.0,
    fee_currency: str = "USDT",
    realized_pnl: float | None = None,
    trade_id: int = 0,
    symbol: str = SYMBOL,
) -> SimpleNamespace:
    return SimpleNamespace(
        side=side,
        price=price,
        amount=amount,
        fee_cost=fee_cost,
        fee_currency=fee_currency,
        realized_pnl=realized_pnl,
        traded_at=datetime(2026, 6, 1, 0, trade_id % 60, tzinfo=UTC),
        id=trade_id,
        symbol=symbol,
    )


# ── 1. realized = Σ exchange realizedPnl, not FIFO recomputed from prices ──────


def test_realized_is_sum_of_exchange_realized_pnl_not_fifo() -> None:
    # Prices imply a 30.0 FIFO gain, but the exchange reports 11.98 — trust it.
    fills = [
        _fill(side="buy", price=100.0, realized_pnl=0.0, trade_id=1),
        _fill(side="sell", price=130.0, realized_pnl=11.98, trade_id=2),
    ]
    breakdown = compute_realized_breakdown(symbol=SYMBOL, trades=fills)
    assert breakdown.gross_realized == pytest.approx(11.98)


# ── 2. funding is included in net realized ────────────────────────────────────


def test_funding_is_folded_into_net_realized() -> None:
    fills = [
        _fill(side="buy", price=100.0, realized_pnl=0.0, trade_id=1),
        _fill(side="sell", price=110.0, realized_pnl=10.0, trade_id=2),
    ]
    paid = compute_realized_breakdown(symbol=SYMBOL, trades=fills, funding=-0.75)
    received = compute_realized_breakdown(symbol=SYMBOL, trades=fills, funding=0.75)
    assert paid.net_realized == pytest.approx(10.0 - 0.75)
    assert received.net_realized == pytest.approx(10.0 + 0.75)


def test_funding_only_position_realizes_funding_minus_commission() -> None:
    # Still-open position (no closing fills): realized is funding − commission.
    fills = [_fill(side="buy", price=100.0, fee_cost=0.08, realized_pnl=0.0, trade_id=1)]
    breakdown = compute_realized_breakdown(symbol=SYMBOL, trades=fills, funding=-0.5)
    assert breakdown.gross_realized == pytest.approx(0.0)
    assert breakdown.commission == pytest.approx(0.08)
    assert breakdown.net_realized == pytest.approx(-0.08 - 0.5)


# ── 3. non-USDT (BNB) commission valued via mark, else conservative 0 ─────────


def test_bnb_commission_valued_via_mark_else_zero() -> None:
    fills = [
        _fill(side="buy", price=100.0, fee_cost=0.01, fee_currency="BNB", realized_pnl=0.0),
        _fill(side="sell", price=110.0, fee_cost=0.01, fee_currency="BNB", realized_pnl=10.0),
    ]
    assert compute_realized_breakdown(symbol=SYMBOL, trades=fills).commission == 0.0
    valued = compute_realized_breakdown(symbol=SYMBOL, trades=fills, mark_prices={"BNB": 600.0})
    assert valued.commission == pytest.approx(12.0)


def test_base_asset_commission_valued_via_fill_price() -> None:
    # Fee paid in the base asset (BTC) is valued at the fill price, no mark needed.
    fills = [_fill(side="buy", price=100.0, fee_cost=0.02, fee_currency="BTC", realized_pnl=0.0)]
    assert compute_realized_breakdown(symbol=SYMBOL, trades=fills).commission == pytest.approx(2.0)


# ── 4. SHORT positions: exchange realized_pnl and FIFO fallback both work ─────


def test_short_realized_from_exchange_realized_pnl() -> None:
    fills = [
        _fill(side="sell", price=110.0, realized_pnl=0.0, trade_id=1),  # open short
        _fill(side="buy", price=100.0, realized_pnl=10.0, trade_id=2),  # cover lower → +10
    ]
    result = compute_realized_breakdown(symbol=SYMBOL, trades=fills)
    assert result.gross_realized == pytest.approx(10.0)


def test_short_realized_via_fifo_fallback_when_untagged() -> None:
    fills = [
        _fill(side="sell", price=110.0, trade_id=1),  # untagged open short
        _fill(side="buy", price=100.0, trade_id=2),  # untagged cover → +10 price PnL
    ]
    result = compute_realized_breakdown(symbol=SYMBOL, trades=fills)
    assert result.gross_realized == pytest.approx(10.0)


# ── 5. mixed tagged/untagged fills ────────────────────────────────────────────


def test_mixed_tagged_and_untagged_fills() -> None:
    fills = [
        _fill(side="buy", price=100.0, realized_pnl=None, trade_id=1),  # legacy open, no field
        _fill(side="sell", price=130.0, realized_pnl=7.0, trade_id=2),  # tagged close
    ]
    # tagged contributes 7.0; the untagged open never closes in its subset → 0.
    result = compute_realized_breakdown(symbol=SYMBOL, trades=fills)
    assert result.gross_realized == pytest.approx(7.0)


# ── 6. net identity holds for arbitrary fills + funding ───────────────────────


@pytest.mark.parametrize("funding", [0.0, -1.25, 3.5])
def test_net_equals_gross_minus_commission_plus_funding(funding: float) -> None:
    fills = [
        _fill(side="buy", price=100.0, amount=2.0, fee_cost=0.08, realized_pnl=0.0, trade_id=1),
        _fill(side="sell", price=106.0, amount=1.0, fee_cost=0.05, realized_pnl=12.0, trade_id=2),
    ]
    b = compute_realized_breakdown(symbol=SYMBOL, trades=fills, funding=funding)
    assert b.net_realized == pytest.approx(b.gross_realized - b.commission + funding)


# ── 7. empty fills → all-zero breakdown, no crash ─────────────────────────────


def test_empty_fills_yield_zero_breakdown() -> None:
    b = compute_realized_breakdown(symbol=SYMBOL, trades=[])
    assert (b.gross_realized, b.commission, b.funding) == (0.0, 0.0, 0.0)
    assert b.net_realized == 0.0
    assert b.unrealized is None


# ── 8. the two engines agree on the same fills ────────────────────────────────


def test_trades_view_realized_equals_breakdown_gross() -> None:
    fills = [
        _fill(side="buy", price=100.0, amount=2.0, fee_cost=0.08, realized_pnl=0.0, trade_id=1),
        _fill(side="sell", price=106.0, amount=1.0, fee_cost=0.05, realized_pnl=12.0, trade_id=2),
    ]
    breakdown = compute_realized_breakdown(symbol=SYMBOL, trades=fills, funding=-0.5)
    snapshot = calculate_futures_pnl_fifo(symbol=SYMBOL, trades=fills)
    assert snapshot.realized == pytest.approx(breakdown.gross_realized)


# ── 9. total-fee summing across mixed symbols/currencies ──────────────────────


def test_total_fee_sums_across_symbols_and_currencies() -> None:
    rows = [
        _fill(side="buy", price=100.0, fee_cost=0.5, fee_currency="USDT", symbol="BTC/USDT:USDT"),
        _fill(side="sell", price=3000.0, fee_cost=0.01, fee_currency="BNB", symbol="ETH/USDT:USDT"),
    ]
    assert sum_fee_cost_quote(rows) == pytest.approx(0.5)  # BNB dropped without a mark
    assert sum_fee_cost_quote(rows, {"BNB": 600.0}) == pytest.approx(6.5)
