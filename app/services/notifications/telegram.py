"""Thin async Telegram Bot API client built on httpx (no SDK dependency).

Only the handful of methods the notification feature needs: ``sendMessage``,
``setWebhook``/``deleteWebhook`` and ``getMe``. Every Bot API response is a JSON
envelope ``{"ok": bool, "result"/"description", "error_code", "parameters"}``;
``send_message`` classifies the outcome so the dispatcher can decide between
retry (rate-limited / transient error) and unlink (forbidden).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.telegram.org"


class TelegramSendStatus(StrEnum):
    SENT = "sent"
    RATE_LIMITED = "rate_limited"
    FORBIDDEN = "forbidden"
    ERROR = "error"


@dataclass(frozen=True)
class TelegramSendResult:
    status: TelegramSendStatus
    retry_after: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is TelegramSendStatus.SENT


class TelegramClient:
    def __init__(
        self,
        *,
        token: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._timeout = timeout
        self._transport = transport
        self._base_url = f"{_API_ROOT}/bot{token}"

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def _post(self, method: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with self._new_client() as client:
            response = await client.post(f"{self._base_url}/{method}", json=payload)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return response.status_code, body

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> TelegramSendResult:
        try:
            status_code, body = await self._post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": disable_web_page_preview,
                },
            )
        except httpx.HTTPError as exc:
            return TelegramSendResult(status=TelegramSendStatus.ERROR, error=str(exc))
        return self._classify(status_code, body)

    @staticmethod
    def _classify(status_code: int, body: dict[str, Any]) -> TelegramSendResult:
        if status_code == 200 and body.get("ok") is True:
            return TelegramSendResult(status=TelegramSendStatus.SENT)
        error_code = body.get("error_code", status_code)
        description = str(body.get("description") or f"HTTP {status_code}")
        if error_code == 429 or status_code == 429:
            params = body.get("parameters")
            retry_after = None
            if isinstance(params, dict):
                raw = params.get("retry_after")
                retry_after = int(raw) if isinstance(raw, (int, float)) else None
            return TelegramSendResult(
                status=TelegramSendStatus.RATE_LIMITED,
                retry_after=retry_after,
                error=description,
            )
        if error_code == 403 or status_code == 403:
            return TelegramSendResult(status=TelegramSendStatus.FORBIDDEN, error=description)
        return TelegramSendResult(status=TelegramSendStatus.ERROR, error=description)

    async def get_me_username(self) -> str | None:
        try:
            _status, body = await self._post("getMe", {})
        except httpx.HTTPError:
            return None
        result = body.get("result")
        if isinstance(result, dict):
            username = result.get("username")
            if isinstance(username, str):
                return username
        return None

    async def set_webhook(
        self,
        *,
        url: str,
        secret_token: str,
        allowed_updates: list[str] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"url": url, "secret_token": secret_token}
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        try:
            status_code, body = await self._post("setWebhook", payload)
        except httpx.HTTPError as exc:
            logger.warning("setWebhook failed: %s", exc)
            return False
        return status_code == 200 and body.get("ok") is True

    async def delete_webhook(self) -> bool:
        try:
            status_code, body = await self._post("deleteWebhook", {})
        except httpx.HTTPError:
            return False
        return status_code == 200 and body.get("ok") is True
