from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1.endpoints import live as live_endpoint
from app.api.v1.endpoints import telegram_webhook as webhook_endpoint
from app.core.config import Settings
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base
from app.models.telegram_notification_settings import TelegramNotificationSettings
from app.models.user import User
from app.services.notifications.service import TelegramNotificationService
from app.services.notifications.telegram import TelegramSendResult, TelegramSendStatus


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_message(self, *, chat_id: int, text: str, **_: object) -> TelegramSendResult:
        self.calls.append({"chat_id": chat_id, "text": text})
        return TelegramSendResult(status=TelegramSendStatus.SENT)


@pytest.fixture
async def notif_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "telegram_endpoints.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                User(id=1, email="u1@x.io", hashed_password="x", is_active=True)
            )
            await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db_session(notif_db: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with notif_db() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session
    yield
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture(autouse=True)
def override_current_user() -> Iterator[None]:
    async def _fake_current_user() -> User:
        return User(id=1, email="u1@x.io", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def install_fake_service(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = _FakeClient()
    service = TelegramNotificationService(client=client, bot_username="my_trade_bot")
    monkeypatch.setattr(live_endpoint, "telegram_notify_service", service)
    monkeypatch.setattr(webhook_endpoint, "telegram_notify_service", service)
    fake_settings = Settings(
        _env_file=None,
        telegram_bot_token="x",
        telegram_webhook_secret="testsecret",
    )
    monkeypatch.setattr(webhook_endpoint, "get_settings", lambda: fake_settings)
    return client


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_get_settings_defaults_to_unlinked() -> None:
    async with _client() as http:
        resp = await http.get("/api/v1/live/notifications/telegram")
    assert resp.status_code == 200
    body = resp.json()
    assert body["linked"] is False
    assert body["enabled"] is False


async def test_link_returns_deep_link_and_persists_code(
    notif_db: async_sessionmaker[AsyncSession],
) -> None:
    async with _client() as http:
        resp = await http.post("/api/v1/live/notifications/telegram/link")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"]
    assert body["deep_link"] == f"https://t.me/my_trade_bot?start={body['code']}"

    async with notif_db() as session:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == 1
            )
        )
    assert row is not None and row.link_code == body["code"]


async def test_update_settings_toggles_flags() -> None:
    async with _client() as http:
        resp = await http.put(
            "/api/v1/live/notifications/telegram",
            json={"enabled": True, "notify_on_close": False},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["notify_on_close"] is False


async def test_delete_unlinks(notif_db: async_sessionmaker[AsyncSession]) -> None:
    async with notif_db() as session:
        session.add(
            TelegramNotificationSettings(user_id=1, chat_id=999, enabled=True)
        )
        await session.commit()

    async with _client() as http:
        resp = await http.delete("/api/v1/live/notifications/telegram")
    assert resp.status_code == 200

    async with notif_db() as session:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == 1
            )
        )
    assert row is not None and row.chat_id is None and row.enabled is False


async def test_webhook_start_links_chat(
    notif_db: async_sessionmaker[AsyncSession],
    install_fake_service: _FakeClient,
) -> None:
    # Seed a pending link code.
    async with notif_db() as session:
        session.add(TelegramNotificationSettings(user_id=1, link_code="abc123"))
        await session.commit()

    async with _client() as http:
        resp = await http.post(
            "/api/v1/telegram/webhook/testsecret",
            headers={"X-Telegram-Bot-Api-Secret-Token": "testsecret"},
            json={
                "update_id": 1,
                "message": {"text": "/start abc123", "chat": {"id": 4242}},
            },
        )
    assert resp.status_code == 200

    async with notif_db() as session:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == 1
            )
        )
    assert row is not None and row.chat_id == 4242 and row.enabled is True
    # A confirmation message was sent back to the chat.
    assert any(c["chat_id"] == 4242 for c in install_fake_service.calls)


async def test_webhook_rejects_bad_secret() -> None:
    async with _client() as http:
        resp = await http.post(
            "/api/v1/telegram/webhook/wrong",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            json={"update_id": 1, "message": {"text": "/start x", "chat": {"id": 1}}},
        )
    assert resp.status_code == 403
