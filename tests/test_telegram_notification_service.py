from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auto_trade_event import AutoTradeEvent
from app.models.base import Base
from app.models.telegram_notification_delivery import TelegramNotificationDelivery
from app.models.telegram_notification_settings import TelegramNotificationSettings
from app.models.user import User
from app.services.notifications.service import TelegramNotificationService
from app.services.notifications.telegram import TelegramSendResult, TelegramSendStatus


class _FakeClient:
    """Records sends; returns a preset result, or a queue of results."""

    def __init__(self, *, results: list[TelegramSendResult] | None = None) -> None:
        self._results = results or [TelegramSendResult(status=TelegramSendStatus.SENT)]
        self.calls: list[dict[str, object]] = []

    async def send_message(self, *, chat_id: int, text: str, **_: object) -> TelegramSendResult:
        self.calls.append({"chat_id": chat_id, "text": text})
        if len(self._results) == 1:
            return self._results[0]
        return self._results[min(len(self.calls) - 1, len(self._results) - 1)]


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "telegram_service.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _service(client: object) -> TelegramNotificationService:
    return TelegramNotificationService(
        client=client,
        lookback_minutes=120,
        bot_username="my_trade_bot",
        link_code_ttl_seconds=900,
        max_attempts=5,
    )


async def _seed_user(session: AsyncSession, user_id: int = 1) -> None:
    session.add(
        User(id=user_id, email=f"u{user_id}@x.io", hashed_password="x", is_active=True)
    )
    await session.commit()


async def _seed_settings(
    session: AsyncSession,
    *,
    user_id: int = 1,
    chat_id: int | None = 555,
    enabled: bool = True,
    notify_on_open: bool = True,
) -> None:
    session.add(
        TelegramNotificationSettings(
            user_id=user_id,
            chat_id=chat_id,
            enabled=enabled,
            notify_on_open=notify_on_open,
        )
    )
    await session.commit()


async def _seed_open_event(session: AsyncSession, *, user_id: int = 1) -> int:
    event = AutoTradeEvent(
        user_id=user_id,
        event_type="position_opened",
        level="info",
        message="Position opened from signal.",
        payload={"symbol": "BTC/USDT", "trend": "LONG", "entry_price": 64250.0},
        created_at=datetime.now(UTC),
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event.id


# ─────────────────────────────── dispatch ─────────────────────────────────


async def test_dispatch_sends_for_linked_enabled_user_and_records_delivery(
    db: async_sessionmaker[AsyncSession],
) -> None:
    client = _FakeClient()
    async with db() as session:
        await _seed_user(session)
        await _seed_settings(session)
        event_id = await _seed_open_event(session)

    async with db() as session:
        stats = await _service(client).dispatch_pending(session=session)

    assert stats["sent"] == 1
    assert len(client.calls) == 1
    assert client.calls[0]["chat_id"] == 555
    assert "BTC/USDT" in str(client.calls[0]["text"])

    async with db() as session:
        delivery = await session.get(TelegramNotificationDelivery, event_id)
    assert delivery is not None
    assert delivery.status == "sent"
    assert delivery.sent_at is not None


async def test_dispatch_is_idempotent_across_runs(
    db: async_sessionmaker[AsyncSession],
) -> None:
    client = _FakeClient()
    async with db() as session:
        await _seed_user(session)
        await _seed_settings(session)
        await _seed_open_event(session)

    async with db() as session:
        await _service(client).dispatch_pending(session=session)
    async with db() as session:
        stats2 = await _service(client).dispatch_pending(session=session)

    assert stats2["sent"] == 0
    assert len(client.calls) == 1  # not re-sent


async def test_dispatch_skips_disabled_user(db: async_sessionmaker[AsyncSession]) -> None:
    client = _FakeClient()
    async with db() as session:
        await _seed_user(session)
        await _seed_settings(session, enabled=False)
        event_id = await _seed_open_event(session)

    async with db() as session:
        stats = await _service(client).dispatch_pending(session=session)

    assert stats["sent"] == 0
    assert stats["skipped"] == 1
    assert len(client.calls) == 0
    async with db() as session:
        delivery = await session.get(TelegramNotificationDelivery, event_id)
    assert delivery is not None and delivery.status == "skipped"


async def test_dispatch_skips_when_family_toggle_off(
    db: async_sessionmaker[AsyncSession],
) -> None:
    client = _FakeClient()
    async with db() as session:
        await _seed_user(session)
        await _seed_settings(session, notify_on_open=False)
        await _seed_open_event(session)

    async with db() as session:
        stats = await _service(client).dispatch_pending(session=session)

    assert stats["skipped"] == 1
    assert len(client.calls) == 0


async def test_dispatch_skips_unlinked_user(db: async_sessionmaker[AsyncSession]) -> None:
    client = _FakeClient()
    async with db() as session:
        await _seed_user(session)
        await _seed_settings(session, chat_id=None)
        await _seed_open_event(session)

    async with db() as session:
        stats = await _service(client).dispatch_pending(session=session)

    assert stats["skipped"] == 1
    assert len(client.calls) == 0


async def test_dispatch_forbidden_unlinks_user(db: async_sessionmaker[AsyncSession]) -> None:
    client = _FakeClient(results=[TelegramSendResult(status=TelegramSendStatus.FORBIDDEN)])
    async with db() as session:
        await _seed_user(session)
        await _seed_settings(session)
        await _seed_open_event(session)

    async with db() as session:
        await _service(client).dispatch_pending(session=session)

    async with db() as session:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == 1
            )
        )
    assert row is not None
    assert row.chat_id is None
    assert row.enabled is False


async def test_dispatch_retries_failed_event_until_sent(
    db: async_sessionmaker[AsyncSession],
) -> None:
    client = _FakeClient(
        results=[
            TelegramSendResult(status=TelegramSendStatus.ERROR, error="boom"),
            TelegramSendResult(status=TelegramSendStatus.SENT),
        ]
    )
    async with db() as session:
        await _seed_user(session)
        await _seed_settings(session)
        event_id = await _seed_open_event(session)

    async with db() as session:
        stats1 = await _service(client).dispatch_pending(session=session)
    assert stats1["failed"] == 1
    async with db() as session:
        delivery = await session.get(TelegramNotificationDelivery, event_id)
    assert delivery is not None and delivery.status == "failed" and delivery.attempts == 1

    async with db() as session:
        stats2 = await _service(client).dispatch_pending(session=session)
    assert stats2["sent"] == 1
    async with db() as session:
        delivery = await session.get(TelegramNotificationDelivery, event_id)
    assert delivery is not None and delivery.status == "sent"


async def test_dispatch_noop_without_client(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await _seed_user(session)
        await _seed_settings(session)
        await _seed_open_event(session)

    service = TelegramNotificationService(client=None, bot_username="")
    async with db() as session:
        stats = await service.dispatch_pending(session=session)
    assert stats["sent"] == 0


# ─────────────────────────────── linking ──────────────────────────────────


async def test_generate_link_then_handle_start_links_chat(
    db: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(_FakeClient())
    async with db() as session:
        await _seed_user(session)
        link = await service.generate_link(session=session, user_id=1)

    assert link["code"]
    assert link["deep_link"] == f"https://t.me/my_trade_bot?start={link['code']}"

    async with db() as session:
        ok = await service.handle_start(session=session, code=str(link["code"]), chat_id=777)
    assert ok is True

    async with db() as session:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == 1
            )
        )
    assert row is not None
    assert row.chat_id == 777
    assert row.enabled is True
    assert row.link_code is None


async def test_handle_start_rejects_unknown_code(
    db: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(_FakeClient())
    async with db() as session:
        await _seed_user(session)
        ok = await service.handle_start(session=session, code="nope", chat_id=777)
    assert ok is False


async def test_handle_start_rejects_expired_code(
    db: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(_FakeClient())
    async with db() as session:
        await _seed_user(session)
        session.add(
            TelegramNotificationSettings(
                user_id=1,
                link_code="expired",
                link_code_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.commit()
        ok = await service.handle_start(session=session, code="expired", chat_id=777)
    assert ok is False


# ───────────────────────── webhook install (glue) ─────────────────────────


async def test_install_webhook_noop_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings
    from app.services.notifications import service as service_mod

    monkeypatch.setattr(service_mod, "get_settings", lambda: Settings(_env_file=None))
    assert await service_mod.install_telegram_webhook() is False


async def test_install_webhook_noop_without_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import Settings
    from app.services.notifications import service as service_mod

    # Token set but no public base URL / secret → cannot install, returns False.
    monkeypatch.setattr(
        service_mod,
        "get_settings",
        lambda: Settings(_env_file=None, telegram_bot_token="123:abc"),
    )

    async def _fake_get_me(self: object) -> str | None:
        return "my_trade_bot"

    monkeypatch.setattr(
        "app.services.notifications.telegram.TelegramClient.get_me_username",
        _fake_get_me,
    )
    assert await service_mod.install_telegram_webhook() is False
