from datetime import UTC, datetime

from app.schemas.exchange_trading import NormalizedBalance, NormalizedTrade
from app.services.execution.pnl import calculate_spot_pnl


def test_calculate_spot_pnl_fifo_with_fees() -> None:
    trades = [
        NormalizedTrade(
            id="1",
            symbol="BTC/USDT",
            side="buy",
            amount=1.0,
            price=100.0,
            fee_cost=1.0,
            fee_currency="USDT",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        NormalizedTrade(
            id="2",
            symbol="BTC/USDT",
            side="sell",
            amount=0.4,
            price=130.0,
            fee_cost=0.5,
            fee_currency="USDT",
            timestamp=datetime(2026, 1, 2, tzinfo=UTC),
        ),
    ]
    balances = [
        NormalizedBalance(asset="BTC", free=0.6, used=0.0, total=0.6),
        NormalizedBalance(asset="USDT", free=500.0, used=0.0, total=500.0),
    ]
    assets, realized, unrealized, fees = calculate_spot_pnl(
        trades=trades,
        balances=balances,
        quote_asset="USDT",
        mark_prices={"BTC": 140.0},
    )

    assert len(assets) == 1
    btc = assets[0]
    assert btc.asset == "BTC"
    assert btc.quantity == 0.6
    assert btc.average_entry_price == 101.0
    assert round(realized, 2) == 11.1
    assert round(unrealized, 2) == 23.4
    assert round(fees, 2) == 1.5


def test_calculate_spot_pnl_converts_base_fee_to_quote() -> None:
    trades = [
        NormalizedTrade(
            id="1",
            symbol="BTC/USDT",
            side="buy",
            amount=1.0,
            price=100.0,
            fee_cost=0.01,
            fee_currency="BTC",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    balances = [
        NormalizedBalance(asset="BTC", free=0.99, used=0.0, total=0.99),
        NormalizedBalance(asset="USDT", free=0.0, used=0.0, total=0.0),
    ]
    _, _, _, fees = calculate_spot_pnl(
        trades=trades,
        balances=balances,
        quote_asset="USDT",
        mark_prices={"BTC": 100.0},
    )

    assert round(fees, 2) == 1.0
