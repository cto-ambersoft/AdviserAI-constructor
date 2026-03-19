from datetime import UTC, datetime

from app.services.execution.account_trades_service import AccountTradesService


class _Row:
    def __init__(self) -> None:
        self.exchange_trade_id = "trade-1"
        self.traded_at = datetime(2026, 3, 9, 10, 12, 40, tzinfo=UTC)
        self.side = "sell"
        self.price = 67952.0
        self.amount = 0.014
        self.fee_cost = 0.5232304
        self.fee_currency = "USDT"
        self.exchange_order_id = "order-1"
        self.origin = "platform"
        self.raw_trade = {"id": "trade-1"}


class _State:
    def __init__(self) -> None:
        self.last_trade_id = "trade-1"
        self.last_trade_ts_ms = 1773051160710


class _SyncResult:
    def __init__(self) -> None:
        self.warnings: list[str] = []


async def test_account_trades_service_normalizes_symbol(monkeypatch) -> None:
    service = AccountTradesService()
    captured: dict[str, str] = {}

    async def _fake_get_account(*, session, account_id: int, user_id: int):
        return object()

    async def _fake_sync(*, session, user_id: int, account_id: int, symbol: str, market_type: str):
        captured["sync_symbol"] = symbol
        return _SyncResult()

    async def _fake_list(*, session, user_id: int, account_id: int, symbol: str, origin, limit: int):
        captured["list_symbol"] = symbol
        return [_Row()]

    async def _fake_state(*, session, user_id: int, account_id: int, symbol: str, market_type: str):
        captured["state_symbol"] = symbol
        return _State()

    async def _fake_position(*, session, user_id: int, account_id: int, symbol: str):
        captured["position_symbol"] = symbol
        return None

    async def _fake_events(*, session, user_id: int, limit: int, account_id: int | None = None):
        return []

    monkeypatch.setattr(service._credentials, "get_account", _fake_get_account)
    monkeypatch.setattr(service._sync, "sync_account_symbol_trades", _fake_sync)
    monkeypatch.setattr(service._sync, "list_trades", _fake_list)
    monkeypatch.setattr(service._sync, "get_sync_state", _fake_state)
    monkeypatch.setattr(service._trading, "fetch_futures_position", _fake_position)
    monkeypatch.setattr(service._auto_trade, "list_events", _fake_events)

    response = await service.get_account_trades(
        session=None,  # type: ignore[arg-type]
        user_id=1,
        account_id=2,
        symbol="BTCUSDT",
        limit=100,
        events_limit=50,
    )

    assert captured["sync_symbol"] == "BTC/USDT:USDT"
    assert captured["list_symbol"] == "BTC/USDT:USDT"
    assert captured["state_symbol"] == "BTC/USDT:USDT"
    assert captured["position_symbol"] == "BTC/USDT:USDT"
    assert response.symbol == "BTC/USDT:USDT"
    assert len(response.trades) == 1
