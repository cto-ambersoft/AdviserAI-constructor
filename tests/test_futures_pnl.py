from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.exchange_trading import NormalizedFuturesPosition
from app.services.execution.futures_pnl import (
    calculate_futures_pnl_fifo,
    compute_realized_breakdown,
    sum_fee_cost_quote,
)


class _TradeRow:
    def __init__(
        self,
        *,
        side: str,
        price: float,
        amount: float,
        fee_cost: float,
        fee_currency: str | None,
        traded_at: datetime,
        trade_id: int,
        realized_pnl: float | None = None,
    ) -> None:
        self.side = side
        self.price = price
        self.amount = amount
        self.fee_cost = fee_cost
        self.fee_currency = fee_currency
        self.traded_at = traded_at
        self.id = trade_id
        self.realized_pnl = realized_pnl


def test_calculate_futures_pnl_fifo() -> None:
    trades = [
        _TradeRow(
            side="buy",
            price=100.0,
            amount=1.0,
            fee_cost=1.0,
            fee_currency="USDT",
            traded_at=datetime(2026, 1, 1, tzinfo=UTC),
            trade_id=1,
        ),
        _TradeRow(
            side="sell",
            price=130.0,
            amount=0.4,
            fee_cost=0.5,
            fee_currency="USDT",
            traded_at=datetime(2026, 1, 2, tzinfo=UTC),
            trade_id=2,
        ),
    ]
    pnl = calculate_futures_pnl_fifo(symbol="BTC/USDT:USDT", trades=trades)
    # realized is now gross price PnL (30 * 0.4 = 12.0); the 1.5 in fees is
    # reported separately as commission, so net is still 12.0 − 1.5 = 10.5.
    assert round(pnl.realized, 2) == 12.0
    assert pnl.base_currency == "BTC"
    assert pnl.quote_currency == "USDT"


def test_futures_pnl_uses_exchange_realized_when_present() -> None:
    """When fills carry the exchange's realized_pnl, realized is the sum of
    those authoritative values — not FIFO recomputed from prices."""
    trades = [
        _TradeRow(
            side="buy",
            price=100.0,
            amount=1.0,
            fee_cost=0.04,
            fee_currency="USDT",
            traded_at=datetime(2026, 1, 1, tzinfo=UTC),
            trade_id=1,
            realized_pnl=0.0,  # opening fill — authoritative zero
        ),
        _TradeRow(
            side="sell",
            price=130.0,
            amount=1.0,
            fee_cost=0.05,
            fee_currency="USDT",
            traded_at=datetime(2026, 1, 2, tzinfo=UTC),
            trade_id=2,
            realized_pnl=11.98,  # closing fill — exchange-reported, != price FIFO (29.91)
        ),
    ]
    pnl = calculate_futures_pnl_fifo(symbol="BTC/USDT:USDT", trades=trades)
    assert round(pnl.realized, 2) == 11.98


def test_futures_pnl_mixed_rows_sum_authoritative_plus_fifo_fallback() -> None:
    """Untagged (legacy) fills fall back to FIFO; tagged fills use realized_pnl.
    The opening buy has no realized_pnl and never closes in the subset, so it
    contributes 0 — realized is the authoritative 5.0 from the closing fill."""
    trades = [
        _TradeRow(
            side="buy",
            price=100.0,
            amount=1.0,
            fee_cost=0.0,
            fee_currency="USDT",
            traded_at=datetime(2026, 1, 1, tzinfo=UTC),
            trade_id=1,
            realized_pnl=None,  # legacy fill, field absent
        ),
        _TradeRow(
            side="sell",
            price=130.0,
            amount=1.0,
            fee_cost=0.0,
            fee_currency="USDT",
            traded_at=datetime(2026, 1, 2, tzinfo=UTC),
            trade_id=2,
            realized_pnl=5.0,
        ),
    ]
    pnl = calculate_futures_pnl_fifo(symbol="BTC/USDT:USDT", trades=trades)
    assert round(pnl.realized, 2) == 5.0


def test_calculate_futures_pnl_uses_live_unrealized_when_available() -> None:
    trades = [
        _TradeRow(
            side="buy",
            price=100.0,
            amount=1.0,
            fee_cost=0.0,
            fee_currency="USDT",
            traded_at=datetime(2026, 1, 1, tzinfo=UTC),
            trade_id=1,
        )
    ]
    position = NormalizedFuturesPosition(
        symbol="BTC/USDT:USDT",
        side="long",
        contracts=1.0,
        entry_price=100.0,
        mark_price=140.0,
        unrealized_pnl=42.0,
    )
    pnl = calculate_futures_pnl_fifo(
        symbol="BTC/USDT:USDT",
        trades=trades,
        live_position=position,
    )
    assert pnl.unrealized == 42.0


def test_trades_engine_and_breakdown_share_one_source() -> None:
    """The /accounts trades view (calculate_futures_pnl_fifo) and the shared
    breakdown engine agree on the same fills: realized == gross, FIFO is only a
    fallback for untagged fills, and net = gross − commission + funding."""
    trades = [
        _TradeRow(
            side="buy", price=100.0, amount=2.0, fee_cost=0.08, fee_currency="USDT",
            traded_at=datetime(2026, 1, 1, tzinfo=UTC), trade_id=1, realized_pnl=0.0,
        ),
        _TradeRow(
            side="sell", price=106.0, amount=1.0, fee_cost=0.05, fee_currency="USDT",
            traded_at=datetime(2026, 1, 2, tzinfo=UTC), trade_id=2, realized_pnl=12.0,
        ),
    ]
    breakdown = compute_realized_breakdown(
        symbol="BTC/USDT:USDT", trades=trades, funding=-0.5
    )
    snapshot = calculate_futures_pnl_fifo(symbol="BTC/USDT:USDT", trades=trades)

    # Both derive realized from the same engine: gross exchange realized_pnl.
    assert snapshot.realized == breakdown.gross_realized == 12.0
    assert breakdown.commission == 0.13  # exchange fees, not folded into gross
    assert breakdown.funding == -0.5
    assert breakdown.net_realized == (12.0 - 0.13 - 0.5)


def test_commission_values_non_usdt_fee_via_mark_price() -> None:
    """BNB-paid commission (the 25% discount case) must be valued via a mark
    price, not silently dropped to 0 — otherwise PnL is overstated."""
    trades = [
        _TradeRow(
            side="buy", price=100.0, amount=1.0, fee_cost=0.01, fee_currency="BNB",
            traded_at=datetime(2026, 1, 1, tzinfo=UTC), trade_id=1, realized_pnl=0.0,
        ),
        _TradeRow(
            side="sell", price=110.0, amount=1.0, fee_cost=0.01, fee_currency="BNB",
            traded_at=datetime(2026, 1, 2, tzinfo=UTC), trade_id=2, realized_pnl=10.0,
        ),
    ]
    # Without a BNB mark, the fee asset can't be valued → 0 (no crash).
    without_mark = compute_realized_breakdown(symbol="BTC/USDT:USDT", trades=trades)
    assert without_mark.commission == 0.0

    # With a BNB/USDT mark of 600: commission = 0.02 BNB * 600 = 12.0.
    with_mark = compute_realized_breakdown(
        symbol="BTC/USDT:USDT", trades=trades, mark_prices={"BNB": 600.0}
    )
    assert with_mark.commission == pytest.approx(12.0)
    assert with_mark.net_realized == pytest.approx(10.0 - 12.0)


def test_sum_fee_cost_quote_multi_currency() -> None:
    """Total fees across mixed fee currencies and symbols: USDT direct, BNB via
    a supplied mark price; non-USDT without a mark contributes 0 (no crash)."""
    rows = [
        SimpleNamespace(symbol="BTC/USDT:USDT", fee_cost=0.5, fee_currency="USDT", price=100.0),
        SimpleNamespace(symbol="ETH/USDT:USDT", fee_cost=0.01, fee_currency="BNB", price=3000.0),
    ]
    # Without a BNB mark only the USDT fee counts.
    assert sum_fee_cost_quote(rows) == pytest.approx(0.5)
    # With a BNB mark the BNB fee is valued: 0.5 + 0.01 * 600 = 6.5.
    assert sum_fee_cost_quote(rows, {"BNB": 600.0}) == pytest.approx(6.5)
