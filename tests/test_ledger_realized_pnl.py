"""T1 — authoritative realized PnL extraction + ledger backfill.

Binance USDⓈ-M ``userTrades`` carries a per-fill ``realizedPnl`` string (gross
price PnL, excluding commission/funding — confirmed against Binance docs). It is
``"0"`` on opening fills and non-zero on closing fills, and lives under
``raw_trade.info.realizedPnl``. ``extract_realized_pnl`` lifts it; the migration
backfills the new column from already-stored ``raw_trade`` JSON.
"""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.exchange_trade_ledger import ExchangeTradeLedger
from app.services.auto_trade.trade_sync import extract_realized_pnl


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"info": {"realizedPnl": "2.58500000"}}, 2.585),
        ({"info": {"realizedPnl": "0"}}, 0.0),  # opening fill — authoritative zero, not None
        ({"info": {"realizedPnl": "-0.91539999"}}, -0.91539999),
        ({"realizedPnl": "1.25"}, 1.25),  # top-level fallback
        ({"info": {"realizedPnl": "abc"}}, None),  # non-numeric
        ({"info": {"realizedPnl": None}}, None),
        ({"info": {}}, None),  # field absent
        ({}, None),
        (None, None),
        ("not-a-dict", None),
    ],
)
def test_extract_realized_pnl(raw: object, expected: float | None) -> None:
    result = extract_realized_pnl(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


async def test_backfill_realized_pnl_from_raw_trade(tmp_path: Path) -> None:
    """The migration backfill populates realized_pnl from stored raw_trade JSON,
    setting 0.0 for opening fills and leaving rows without the field as NULL."""
    import importlib.util

    migration_path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "20260604_0022_add_ledger_realized_pnl.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0022", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    base_row = dict(
        user_id=1,
        account_id=1,
        exchange_name="binance",
        market_type="futures",
        symbol="BTC/USDT:USDT",
        exchange_order_id="o",
        client_order_id=None,
        side="sell",
        price=100.0,
        amount=1.0,
        cost=100.0,
        fee_cost=0.04,
        fee_currency="USDT",
        traded_at=__import__("datetime").datetime(2026, 6, 1, tzinfo=__import__("datetime").UTC),
        ingested_at=__import__("datetime").datetime(2026, 6, 1, tzinfo=__import__("datetime").UTC),
        origin="platform",
        origin_confidence="strong",
    )
    async with factory() as session:
        session.add(
            ExchangeTradeLedger(
                exchange_trade_id="close-1",
                raw_trade={"info": {"realizedPnl": "11.98"}},
                **base_row,
            )
        )
        session.add(
            ExchangeTradeLedger(
                exchange_trade_id="open-1",
                raw_trade={"info": {"realizedPnl": "0"}},
                **base_row,
            )
        )
        session.add(
            ExchangeTradeLedger(
                exchange_trade_id="legacy-1",
                raw_trade={"info": {}},  # no realizedPnl — stays NULL
                **base_row,
            )
        )
        await session.commit()

    async with engine.begin() as conn:
        updated = await conn.run_sync(migration.backfill_realized_pnl)
    assert updated == 2  # the close (11.98) and the open (0.0)

    async with factory() as session:
        rows = {
            r.exchange_trade_id: r.realized_pnl
            for r in (await session.scalars(select(ExchangeTradeLedger))).all()
        }
    assert rows["close-1"] == pytest.approx(11.98)
    assert rows["open-1"] == pytest.approx(0.0)
    assert rows["legacy-1"] is None

    await engine.dispose()
