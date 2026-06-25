from datetime import datetime

from pydantic import BaseModel


class TelegramSettingsOut(BaseModel):
    linked: bool
    enabled: bool
    notify_on_open: bool
    notify_on_close: bool
    notify_on_risk: bool
    linked_at: datetime | None = None


class TelegramSettingsUpdate(BaseModel):
    enabled: bool | None = None
    notify_on_open: bool | None = None
    notify_on_close: bool | None = None
    notify_on_risk: bool | None = None


class TelegramLinkOut(BaseModel):
    code: str
    deep_link: str | None = None
    expires_at: datetime | None = None


class TelegramTestResult(BaseModel):
    status: str
    error: str | None = None
