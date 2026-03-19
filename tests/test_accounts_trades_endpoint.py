from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.v1.endpoints import accounts
from app.main import app
from app.models.user import User
from app.schemas.exchange_trading import (
    AccountAutoTradeEventRead,
    AccountTradeRead,
    AccountTradesPnlRead,
    AccountTradesRead,
    AccountTradesSyncStateRead,
)


async def _fake_current_user() -> User:
    return User(id=1, email="accounts-test@example.com", hashed_password="x", is_active=True)


async def test_accounts_trades_endpoint(monkeypatch) -> None:
    async def _fake_get_account_trades(
        *,
        session,
        user_id: int,
        account_id: int,
        symbol: str,
        limit: int,
        events_limit: int,
    ) -> AccountTradesRead:
        assert user_id == 1
        assert account_id == 7
        assert symbol == "BTC/USDT:USDT"
        assert limit == 150
        assert events_limit == 10
        return AccountTradesRead(
            account_id=account_id,
            symbol="BTC/USDT:USDT",
            trades=[
                AccountTradeRead(
                    exchange_trade_id="t-1",
                    timestamp=datetime.now(UTC),
                    side="buy",
                    price=100.0,
                    amount=0.5,
                    fee=0.01,
                    fee_currency="USDT",
                    order_id="o-1",
                    is_autotrade=True,
                    raw={"id": "t-1"},
                )
            ],
            pnl=AccountTradesPnlRead(
                realized=10.0,
                unrealized=5.0,
                base_currency="BTC",
                quote_currency="USDT",
            ),
            sync_state=AccountTradesSyncStateRead(
                last_trade_id="t-1",
                last_trade_ts=datetime.now(UTC),
            ),
            auto_trade_events=[
                AccountAutoTradeEventRead(
                    id=1,
                    event_type="position_opened",
                    level="info",
                    message="opened",
                    created_at=datetime.now(UTC),
                    payload={"ok": True},
                )
            ],
            sync_warnings=[],
        )

    app.dependency_overrides[get_current_user] = _fake_current_user
    monkeypatch.setattr(accounts.account_trades_service, "get_account_trades", _fake_get_account_trades)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/accounts/7/trades",
                params={"symbol": "BTC/USDT:USDT", "limit": 150, "events_limit": 10},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["account_id"] == 7
        assert body["symbol"] == "BTC/USDT:USDT"
        assert body["trades"][0]["is_autotrade"] is True
        assert body["pnl"]["realized"] == 10.0
    finally:
        app.dependency_overrides.pop(get_current_user, None)
