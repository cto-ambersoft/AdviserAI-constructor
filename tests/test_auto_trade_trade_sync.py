from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.base import Base
from app.models.exchange import ExchangeCredential
from app.models.exchange_order_metadata import ExchangeOrderMetadata
from app.models.exchange_trade_ledger import ExchangeTradeLedger
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.schemas.exchange_trading import NormalizedTrade
from app.services.auto_trade.trade_sync import ExchangeTradeSyncService


class _FakeTradingService:
    def __init__(self) -> None:
        self._page_calls = 0

    async def fetch_futures_trades_page(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        account_id: int,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[NormalizedTrade], str | None]:
        self._page_calls += 1
        if self._page_calls > 1:
            return [], None
        trade = NormalizedTrade(
            id="trade-1",
            order_id="ord-1",
            symbol=symbol,
            side="buy",
            amount=0.5,
            price=100.0,
            cost=50.0,
            fee_cost=0.01,
            fee_currency="USDT",
            timestamp=datetime.now(UTC),
            raw={"clientOrderId": "cid-1"},
        )
        return [trade], "trade-1"


async def _seed_core(session: AsyncSession) -> tuple[User, ExchangeCredential, AutoTradeConfig]:
    user = User(email="sync@example.com", hashed_password="x", is_active=True)
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
    await session.flush()
    account = ExchangeCredential(
        user_id=user.id,
        exchange_name="bybit",
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
    await session.flush()
    position = AutoTradePosition(
        user_id=user.id,
        config_id=config.id,
        profile_id=profile.id,
        account_id=account.id,
        symbol="BTC/USDT:USDT",
        side="LONG",
        status="open",
        entry_price=100.0,
        quantity=0.5,
        position_size_usdt=50.0,
        leverage=1,
        tp_price=110.0,
        sl_price=95.0,
        entry_confidence_pct=70.0,
        opened_at=datetime.now(UTC),
        closed_at=None,
        close_reason=None,
        close_price=None,
        open_order_id="ord-1",
        close_order_id=None,
        open_history_id=None,
        close_history_id=None,
        raw_open_order={},
        raw_close_order={},
    )
    session.add(position)
    await session.flush()
    session.add(
        ExchangeOrderMetadata(
            user_id=user.id,
            account_id=account.id,
            exchange_name="bybit",
            symbol="BTC/USDT:USDT",
            exchange_order_id="ord-1",
            client_order_id="cid-1",
            source="auto_trade_open",
            config_id=config.id,
            position_id=position.id,
            history_id=None,
        )
    )
    await session.commit()
    await session.refresh(account)
    await session.refresh(config)
    return user, account, config


async def test_exchange_trade_sync_upserts_and_marks_platform(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "trade_sync.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as session:
            user, account, config = await _seed_core(session)
            fake = _FakeTradingService()
            service = ExchangeTradeSyncService(trading_service=fake)
            inserted = await service.sync_config_trades(session=session, config=config)
            assert inserted == 1

            rows = list((await session.scalars(select(ExchangeTradeLedger))).all())
            assert len(rows) == 1
            assert rows[0].origin == "platform"
            assert rows[0].origin_confidence == "strong"
            assert rows[0].auto_trade_config_id == config.id

            inserted_second = await service.sync_symbol_trades(
                session=session,
                user_id=user.id,
                account_id=account.id,
                symbol="BTC/USDT:USDT",
                market_type="futures",
                backfill_days=30,
            )
            assert inserted_second == 0
            count = int((await session.scalar(select(func.count(ExchangeTradeLedger.id)))) or 0)
            assert count == 1
    finally:
        await engine.dispose()


async def test_sync_account_symbol_trades_first_and_incremental(tmp_path: Path) -> None:
    db_path = tmp_path / "trade_sync_account.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as session:
            user, account, _ = await _seed_core(session)
            fake = _FakeTradingService()
            service = ExchangeTradeSyncService(trading_service=fake)

            first = await service.sync_account_symbol_trades(
                session=session,
                user_id=user.id,
                account_id=account.id,
                symbol="BTC/USDT:USDT",
                market_type="futures",
            )
            assert first.inserted_or_updated == 1
            assert first.warnings == []

            second = await service.sync_account_symbol_trades(
                session=session,
                user_id=user.id,
                account_id=account.id,
                symbol="BTC/USDT:USDT",
                market_type="futures",
            )
            assert second.inserted_or_updated == 0
            count = int((await session.scalar(select(func.count(ExchangeTradeLedger.id)))) or 0)
            assert count == 1
    finally:
        await engine.dispose()
