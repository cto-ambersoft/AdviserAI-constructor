"""T5 — income (funding) sync: adapter normalization + idempotent ledger sync.

Mirrors the trade-sync design but for Binance ``/fapi/v1/income`` (FUNDING_FEE
only). Binance-only; non-Binance configs are a no-op.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auto_trade_config import AutoTradeConfig
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.exchange_income_ledger import ExchangeIncomeLedger
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.schemas.exchange_trading import NormalizedIncome
from app.services.auto_trade.income_sync import ExchangeIncomeSyncService, sum_funding
from app.services.execution.base import ExchangeCredentials
from app.services.execution.ccxt_adapter import CcxtAdapter


def test_normalize_income_from_ccxt_funding_structure() -> None:
    adapter = CcxtAdapter(
        ExchangeCredentials(exchange_name="binance", api_key="k", api_secret="s", mode="demo")
    )
    # ccxt funding-history structure: top-level fields + raw row under ``info``.
    payload = {
        "info": {
            "symbol": "ETHUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.00134317",
            "asset": "USDT",
            "time": "1621584000000",
            "info": "FUNDING_FEE",
            "tranId": "4480321991774044580",
            "tradeId": "",
        },
        "symbol": "ETH/USDT:USDT",
        "code": "USDT",
        "timestamp": 1621584000000,
        "id": "4480321991774044580",
        "amount": -0.00134317,
    }
    income = adapter._normalize_income(payload)
    assert income.income_type == "FUNDING_FEE"
    assert income.asset == "USDT"
    assert income.income == -0.00134317
    assert income.tran_id == "4480321991774044580"
    assert income.trade_id is None
    assert income.symbol == "ETHUSDT"
    assert income.timestamp == datetime(2021, 5, 21, 8, 0, tzinfo=UTC)


class _FakeIncomeTradingService:
    """Returns a fixed funding history; ignores ``since`` (page < limit ⇒ one call)."""

    def __init__(self, rows: list[NormalizedIncome]) -> None:
        self.rows = rows
        self.calls = 0

    async def fetch_futures_income(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[NormalizedIncome]:
        self.calls += 1
        return list(self.rows)


async def _seed(session: AsyncSession, *, exchange_name: str) -> AutoTradeConfig:
    user = User(email="inc@example.com", hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()
    profile = PersonalAnalysisProfile(
        user_id=user.id,
        symbol="BTCUSDT",
        query_prompt=None,
        agents={"twitterSentiment": True},
        agent_weights={"twitterSentiment": 1.0},
        interval_minutes=60,
        is_active=True,
        next_run_at=datetime.now(UTC),
        last_triggered_at=None,
        last_completed_at=None,
    )
    session.add(profile)
    account = ExchangeCredential(
        user_id=user.id,
        exchange_name=exchange_name,
        account_label="main",
        mode="demo",
        encrypted_api_key="k",
        encrypted_api_secret="s",
        encrypted_passphrase=None,
    )
    session.add(account)
    await session.flush()
    config = AutoTradeConfig(
        user_id=user.id,
        profile_id=profile.id,
        account_id=account.id,
        enabled=True,
        is_running=True,
        position_size_usdt=100.0,
        leverage=1,
        min_confidence_pct=60.0,
        fast_close_confidence_pct=80.0,
        confirm_reports_required=2,
        risk_mode="1:2",
        sl_pct=1.0,
        tp_pct=2.0,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


def _funding(tran_id: str, income: float, ts: datetime) -> NormalizedIncome:
    return NormalizedIncome(
        income_type="FUNDING_FEE",
        asset="USDT",
        income=income,
        symbol="BTC/USDT:USDT",
        tran_id=tran_id,
        trade_id=None,
        info="FUNDING_FEE",
        timestamp=ts,
        raw={"tranId": tran_id},
    )


async def test_income_sync_inserts_and_is_idempotent(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'income_sync.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as session:
            config = await _seed(session, exchange_name="binance")
            rows = [
                _funding("t1", -0.5, datetime(2026, 6, 1, 0, 0, tzinfo=UTC)),
                _funding("t2", 0.3, datetime(2026, 6, 1, 8, 0, tzinfo=UTC)),
            ]
            service = ExchangeIncomeSyncService(trading_service=_FakeIncomeTradingService(rows))

            inserted = await service.sync_config_income(session=session, config=config)
            assert inserted == 2
            count = int((await session.scalar(select(func.count(ExchangeIncomeLedger.id)))) or 0)
            assert count == 2

            # Re-sync the same funding rows → deduped on tran_id, still 2 rows.
            await service.sync_config_income(session=session, config=config)
            count = int((await session.scalar(select(func.count(ExchangeIncomeLedger.id)))) or 0)
            assert count == 2
    finally:
        await engine.dispose()


async def test_income_sync_skips_non_binance(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'income_bybit.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with factory() as session:
            config = await _seed(session, exchange_name="bybit")
            rows = [_funding("t1", -0.5, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))]
            service = ExchangeIncomeSyncService(trading_service=_FakeIncomeTradingService(rows))

            inserted = await service.sync_config_income(session=session, config=config)
            assert inserted == 0
            count = int((await session.scalar(select(func.count(ExchangeIncomeLedger.id)))) or 0)
            assert count == 0
    finally:
        await engine.dispose()


def _income_row(
    *, tran_id: str, income: float, income_at: datetime, income_type: str = "FUNDING_FEE"
) -> ExchangeIncomeLedger:
    return ExchangeIncomeLedger(
        user_id=1,
        account_id=7,
        exchange_name="binance",
        market_type="futures",
        income_type=income_type,
        asset="USDT",
        income=income,
        symbol="BTC/USDT:USDT",
        tran_id=tran_id,
        trade_id=None,
        info=income_type,
        income_at=income_at,
        ingested_at=income_at,
        raw={},
    )


async def test_sum_funding_signed_and_windowed(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sum_funding.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    jun1 = datetime(2026, 6, 1, tzinfo=UTC)
    jun2 = datetime(2026, 6, 2, tzinfo=UTC)
    try:
        async with factory() as session:
            session.add_all(
                [
                    _income_row(tran_id="f1", income=-0.5, income_at=jun1),
                    _income_row(tran_id="f2", income=0.3, income_at=jun1 + timedelta(hours=8)),
                    _income_row(tran_id="f3", income=-0.2, income_at=jun2),
                    # A non-funding row on the same account/symbol must be ignored.
                    _income_row(
                        tran_id="c1",
                        income=1.0,
                        income_at=jun1 + timedelta(hours=4),
                        income_type="COMMISSION",
                    ),
                ]
            )
            await session.commit()

            # Whole history: signed sum of FUNDING_FEE only (-0.5 + 0.3 - 0.2).
            total = await sum_funding(session=session, account_id=7, symbol="BTC/USDT:USDT")
            assert total == pytest.approx(-0.4)

            # Window [Jun 1, Jun 2): includes f1 + f2, excludes f3 (upper-exclusive).
            windowed = await sum_funding(
                session=session,
                account_id=7,
                symbol="BTC/USDT:USDT",
                start=datetime(2026, 6, 1, tzinfo=UTC),
                end=datetime(2026, 6, 2, tzinfo=UTC),
            )
            assert windowed == pytest.approx(-0.2)

            # No funding for another symbol → 0.0.
            empty = await sum_funding(session=session, account_id=7, symbol="ETH/USDT:USDT")
            assert empty == 0.0
    finally:
        await engine.dispose()
