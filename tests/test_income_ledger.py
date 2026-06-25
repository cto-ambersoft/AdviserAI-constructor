"""T4 — exchange_income_ledger model + identity constraint.

Stores Binance ``/fapi/v1/income`` rows (FUNDING_FEE for now). ``tranId`` is
unique per (user, income type), so the natural idempotency key is
(account_id, exchange_name, income_type, tran_id).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.exchange_income_ledger import ExchangeIncomeLedger


def _income_kwargs(**overrides: object) -> dict[str, object]:
    base = dict(
        user_id=1,
        account_id=1,
        exchange_name="binance",
        market_type="futures",
        income_type="FUNDING_FEE",
        asset="USDT",
        income=0.00134317,
        symbol="BTC/USDT:USDT",
        tran_id="4480321991774044580",
        trade_id=None,
        info="FUNDING_FEE",
        income_at=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        ingested_at=datetime(2026, 6, 1, 8, 1, tzinfo=UTC),
        raw={"incomeType": "FUNDING_FEE", "income": "0.00134317"},
    )
    base.update(overrides)
    return base


async def test_income_ledger_roundtrip(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'income.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as session:
            session.add(ExchangeIncomeLedger(**_income_kwargs()))
            await session.commit()
        async with factory() as session:
            row = (await session.scalars(select(ExchangeIncomeLedger))).one()
            assert row.income_type == "FUNDING_FEE"
            assert row.asset == "USDT"
            assert row.income == pytest.approx(0.00134317)
            assert row.symbol == "BTC/USDT:USDT"
            assert row.tran_id == "4480321991774044580"
    finally:
        await engine.dispose()


async def test_income_ledger_identity_is_unique(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'income_uq.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as session:
            session.add(ExchangeIncomeLedger(**_income_kwargs()))
            # Same (account_id, exchange_name, income_type, tran_id) — different income.
            session.add(ExchangeIncomeLedger(**_income_kwargs(income=99.0)))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()
