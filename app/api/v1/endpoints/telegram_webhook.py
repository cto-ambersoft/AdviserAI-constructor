"""Public Telegram webhook endpoint (no auth).

Telegram delivers updates here after ``setWebhook``. Security rests on an
unguessable path segment plus the ``X-Telegram-Bot-Api-Secret-Token`` header,
both compared against ``telegram_webhook_secret``. We only act on
``/start <link_code>`` messages — everything else is acknowledged and ignored.
The handler always returns 200 (except on a secret mismatch) so Telegram does
not retry.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import DbSession
from app.core.config import get_settings
from app.services.notifications.service import TelegramNotificationService

logger = logging.getLogger(__name__)

router = APIRouter()
telegram_notify_service = TelegramNotificationService()


@router.post("/telegram/webhook/{secret}", include_in_schema=False)
async def telegram_webhook(
    secret: str,
    request: Request,
    session: DbSession,
) -> dict[str, bool]:
    settings = get_settings()
    expected = settings.telegram_webhook_secret
    if expected:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != expected or header != expected:
            raise HTTPException(status_code=403, detail="invalid webhook secret")

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    if not isinstance(update, dict):
        return {"ok": True}

    await _process_update(session, update)
    return {"ok": True}


async def _process_update(session: DbSession, update: dict[str, object]) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return
    text = message.get("text")
    chat = message.get("chat")
    if not isinstance(text, str) or not isinstance(chat, dict):
        return
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return
    if not text.startswith("/start"):
        return

    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        await telegram_notify_service.send_chat_message(
            chat_id=chat_id,
            text="Send the link from the app to connect notifications.",
        )
        return

    linked = await telegram_notify_service.handle_start(
        session=session, code=code, chat_id=chat_id
    )
    reply = (
        "✅ <b>Telegram connected</b>\nTrade notifications are enabled."
        if linked
        else "This link is invalid or expired. Generate a new one in the app."
    )
    await telegram_notify_service.send_chat_message(chat_id=chat_id, text=reply)
