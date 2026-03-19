from datetime import UTC, datetime

from app.schemas.exchange_trading import NormalizedFuturesPosition
from app.services.execution.futures_pnl import calculate_futures_pnl_fifo


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
    ) -> None:
        self.side = side
        self.price = price
        self.amount = amount
        self.fee_cost = fee_cost
        self.fee_currency = fee_currency
        self.traded_at = traded_at
        self.id = trade_id


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
    assert round(pnl.realized, 2) == 10.5
    assert pnl.base_currency == "BTC"
    assert pnl.quote_currency == "USDT"


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
