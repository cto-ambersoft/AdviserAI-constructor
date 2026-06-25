"""Telegram notification service: outbox dispatcher + account linking.

The dispatcher treats ``auto_trade_events`` as a durable outbox. It selects
recent notifiable events that have no terminal delivery yet, checks each
owner's settings/toggles, sends via Telegram, and records the outcome in
``telegram_notification_deliveries`` (keyed by ``event_id``) for idempotency
and bounded retry. It never touches the trading hot path.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.timeutils import as_aware_utc
from app.models.auto_trade_event import AutoTradeEvent
from app.models.telegram_notification_delivery import TelegramNotificationDelivery
from app.models.telegram_notification_settings import TelegramNotificationSettings
from app.services.notifications.formatting import (
    NOTIFIABLE_EVENTS,
    format_event,
    toggle_for_event,
)
from app.services.notifications.telegram import (
    TelegramClient,
    TelegramSendResult,
    TelegramSendStatus,
)

logger = logging.getLogger(__name__)

_UNSET: Any = object()


class _SendClient(Protocol):
    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str = ...,
        disable_web_page_preview: bool = ...,
    ) -> TelegramSendResult: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _build_default_client(token: str, timeout: float) -> TelegramClient | None:
    if not token:
        return None
    return TelegramClient(token=token, timeout=timeout)


class TelegramNotificationService:
    def __init__(
        self,
        *,
        client: Any = _UNSET,
        batch_size: int | None = None,
        max_attempts: int | None = None,
        lookback_minutes: int | None = None,
        link_code_ttl_seconds: int | None = None,
        bot_username: str | None = None,
    ) -> None:
        settings = get_settings()
        self._client: _SendClient | None = (
            _build_default_client(
                settings.telegram_bot_token, settings.telegram_http_timeout_seconds
            )
            if client is _UNSET
            else client
        )
        self._batch_size = batch_size or settings.telegram_notify_batch_size
        self._max_attempts = max_attempts or settings.telegram_notify_max_attempts
        self._lookback = timedelta(
            minutes=lookback_minutes or settings.telegram_notify_lookback_minutes
        )
        self._link_ttl = link_code_ttl_seconds or settings.telegram_link_code_ttl_seconds
        self._bot_username = (
            bot_username if bot_username is not None else settings.telegram_bot_username
        )

    @property
    def configured(self) -> bool:
        """True when sending is possible (a bot token / client is present)."""
        return self._client is not None

    # ──────────────────────────── dispatcher ─────────────────────────────

    async def dispatch_pending(self, *, session: AsyncSession) -> dict[str, int]:
        stats = {"polled": 0, "sent": 0, "skipped": 0, "failed": 0, "errors": 0}
        if self._client is None:
            return stats

        cutoff = _utc_now() - self._lookback
        events = await self._select_candidates(session, cutoff)
        stats["polled"] = len(events)
        if not events:
            return stats

        settings_by_user = await self._load_settings(session, {e.user_id for e in events})
        for event in events:
            try:
                await self._dispatch_one(
                    session=session,
                    event=event,
                    user_settings=settings_by_user.get(event.user_id),
                    stats=stats,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                stats["errors"] += 1
                logger.exception(
                    "telegram dispatch failed for event_id=%s", event.id
                )
        return stats

    async def _select_candidates(
        self, session: AsyncSession, cutoff: datetime
    ) -> list[AutoTradeEvent]:
        stmt = (
            select(AutoTradeEvent)
            .outerjoin(
                TelegramNotificationDelivery,
                TelegramNotificationDelivery.event_id == AutoTradeEvent.id,
            )
            .where(
                AutoTradeEvent.event_type.in_(tuple(NOTIFIABLE_EVENTS)),
                AutoTradeEvent.created_at >= cutoff,
                or_(
                    TelegramNotificationDelivery.event_id.is_(None),
                    and_(
                        TelegramNotificationDelivery.status == "failed",
                        TelegramNotificationDelivery.attempts < self._max_attempts,
                    ),
                ),
            )
            .order_by(AutoTradeEvent.id)
            .limit(self._batch_size)
        )
        return list(await session.scalars(stmt))

    async def _load_settings(
        self, session: AsyncSession, user_ids: set[int]
    ) -> dict[int, TelegramNotificationSettings]:
        if not user_ids:
            return {}
        rows = await session.scalars(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id.in_(tuple(user_ids))
            )
        )
        return {row.user_id: row for row in rows}

    async def _dispatch_one(
        self,
        *,
        session: AsyncSession,
        event: AutoTradeEvent,
        user_settings: TelegramNotificationSettings | None,
        stats: dict[str, int],
    ) -> None:
        delivery = await self._claim_delivery(session, event)
        if delivery is None:
            # Another worker already holds it.
            return

        skip_reason = self._skip_reason(user_settings, event.event_type)
        if skip_reason is not None:
            delivery.status = "skipped"
            delivery.last_error = skip_reason
            stats["skipped"] += 1
            return

        assert user_settings is not None and user_settings.chat_id is not None
        text = format_event(
            event_type=event.event_type,
            payload=dict(event.payload or {}),
            message=event.message,
        )
        result = await self._client.send_message(  # type: ignore[union-attr]
            chat_id=int(user_settings.chat_id), text=text
        )
        self._record_result(
            delivery=delivery,
            result=result,
            user_settings=user_settings,
            stats=stats,
        )

    async def _claim_delivery(
        self, session: AsyncSession, event: AutoTradeEvent
    ) -> TelegramNotificationDelivery | None:
        delivery = await session.get(TelegramNotificationDelivery, event.id)
        if delivery is not None:
            return delivery
        delivery = TelegramNotificationDelivery(
            event_id=event.id, user_id=event.user_id, status="pending", attempts=0
        )
        session.add(delivery)
        try:
            await session.flush()
        except IntegrityError:
            # Lost a race to another worker; skip.
            await session.rollback()
            return None
        return delivery

    def _skip_reason(
        self, user_settings: TelegramNotificationSettings | None, event_type: str
    ) -> str | None:
        if user_settings is None or user_settings.chat_id is None or not user_settings.enabled:
            return "not_enabled"
        family = toggle_for_event(event_type)
        if family == "open" and not user_settings.notify_on_open:
            return "toggle_off"
        if family == "close" and not user_settings.notify_on_close:
            return "toggle_off"
        if family == "risk" and not user_settings.notify_on_risk:
            return "toggle_off"
        return None

    def _record_result(
        self,
        *,
        delivery: TelegramNotificationDelivery,
        result: TelegramSendResult,
        user_settings: TelegramNotificationSettings,
        stats: dict[str, int],
    ) -> None:
        delivery.attempts += 1
        if result.status is TelegramSendStatus.SENT:
            delivery.status = "sent"
            delivery.sent_at = _utc_now()
            delivery.last_error = None
            stats["sent"] += 1
            return
        if result.status is TelegramSendStatus.FORBIDDEN:
            # User blocked the bot — unlink so we stop trying and they re-link.
            delivery.status = "skipped"
            delivery.last_error = result.error or "forbidden"
            user_settings.chat_id = None
            user_settings.enabled = False
            stats["skipped"] += 1
            return
        # RATE_LIMITED or ERROR — leave as failed so it's retried next run
        # (until max_attempts).
        delivery.status = "failed"
        delivery.last_error = result.error or result.status.value
        stats["failed"] += 1

    # ───────────────────────────── linking ───────────────────────────────

    async def _get_or_create_settings(
        self, session: AsyncSession, user_id: int
    ) -> TelegramNotificationSettings:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == user_id
            )
        )
        if row is None:
            row = TelegramNotificationSettings(user_id=user_id)
            session.add(row)
            await session.flush()
        return row

    async def generate_link(
        self, *, session: AsyncSession, user_id: int
    ) -> dict[str, Any]:
        row = await self._get_or_create_settings(session, user_id)
        code = secrets.token_urlsafe(12)[:32]
        row.link_code = code
        row.link_code_expires_at = _utc_now() + timedelta(seconds=self._link_ttl)
        await session.commit()
        deep_link = (
            f"https://t.me/{self._bot_username}?start={code}" if self._bot_username else None
        )
        return {
            "code": code,
            "deep_link": deep_link,
            "expires_at": row.link_code_expires_at,
        }

    async def handle_start(
        self, *, session: AsyncSession, code: str, chat_id: int
    ) -> bool:
        if not code:
            return False
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.link_code == code
            )
        )
        if row is None:
            return False
        expires_at = as_aware_utc(row.link_code_expires_at)
        if expires_at is not None and expires_at < _utc_now():
            return False
        row.chat_id = chat_id
        row.linked_at = _utc_now()
        row.enabled = True
        row.link_code = None
        row.link_code_expires_at = None
        await session.commit()
        return True

    # ───────────────────────── settings read/write ───────────────────────

    async def get_settings_view(
        self, *, session: AsyncSession, user_id: int
    ) -> dict[str, Any]:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == user_id
            )
        )
        if row is None:
            return {
                "linked": False,
                "enabled": False,
                "notify_on_open": True,
                "notify_on_close": True,
                "notify_on_risk": False,
                "linked_at": None,
            }
        return {
            "linked": row.chat_id is not None,
            "enabled": row.enabled,
            "notify_on_open": row.notify_on_open,
            "notify_on_close": row.notify_on_close,
            "notify_on_risk": row.notify_on_risk,
            "linked_at": row.linked_at,
        }

    async def update_settings(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        enabled: bool | None = None,
        notify_on_open: bool | None = None,
        notify_on_close: bool | None = None,
        notify_on_risk: bool | None = None,
    ) -> dict[str, Any]:
        row = await self._get_or_create_settings(session, user_id)
        if enabled is not None:
            row.enabled = enabled
        if notify_on_open is not None:
            row.notify_on_open = notify_on_open
        if notify_on_close is not None:
            row.notify_on_close = notify_on_close
        if notify_on_risk is not None:
            row.notify_on_risk = notify_on_risk
        await session.commit()
        return await self.get_settings_view(session=session, user_id=user_id)

    async def unlink(self, *, session: AsyncSession, user_id: int) -> None:
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == user_id
            )
        )
        if row is None:
            return
        row.chat_id = None
        row.enabled = False
        row.link_code = None
        row.link_code_expires_at = None
        row.linked_at = None
        await session.commit()

    async def send_chat_message(self, *, chat_id: int, text: str) -> None:
        """Best-effort direct send (e.g. webhook link confirmation)."""
        if self._client is None:
            return
        await self._client.send_message(chat_id=chat_id, text=text)

    async def send_test_message(
        self, *, session: AsyncSession, user_id: int
    ) -> TelegramSendResult:
        if self._client is None:
            return TelegramSendResult(
                status=TelegramSendStatus.ERROR, error="telegram not configured"
            )
        row = await session.scalar(
            select(TelegramNotificationSettings).where(
                TelegramNotificationSettings.user_id == user_id
            )
        )
        if row is None or row.chat_id is None:
            return TelegramSendResult(status=TelegramSendStatus.ERROR, error="not linked")
        return await self._client.send_message(
            chat_id=int(row.chat_id),
            text="✅ <b>Test notification</b>\nTelegram is connected.",
        )


async def install_telegram_webhook() -> bool:
    """Best-effort ``setWebhook`` registration for app startup.

    No-op (returns False) when the bot token, public base URL, or webhook
    secret is missing. Never raises — the caller wraps it so a Telegram outage
    cannot block application startup.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False
    client = TelegramClient(
        token=settings.telegram_bot_token,
        timeout=settings.telegram_http_timeout_seconds,
    )
    if not settings.telegram_bot_username:
        username = await client.get_me_username()
        if username:
            logger.info(
                "telegram bot username resolved via getMe: @%s "
                "(set TELEGRAM_BOT_USERNAME to enable deep links)",
                username,
            )
    if not (settings.telegram_public_base_url and settings.telegram_webhook_secret):
        logger.warning(
            "telegram webhook not installed: set TELEGRAM_PUBLIC_BASE_URL and "
            "TELEGRAM_WEBHOOK_SECRET"
        )
        return False
    url = (
        f"{settings.telegram_public_base_url.rstrip('/')}"
        f"{settings.api_v1_prefix}/telegram/webhook/{settings.telegram_webhook_secret}"
    )
    ok = await client.set_webhook(
        url=url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=["message"],
    )
    logger.info("telegram setWebhook: %s", "ok" if ok else "failed")
    return ok
