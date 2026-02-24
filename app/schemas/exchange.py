from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.exchange import EXCHANGE_MODE_REAL
from app.schemas.exchange_trading import SUPPORTED_EXCHANGE_MODES, SUPPORTED_EXCHANGES


class ExchangeSecretIn(BaseModel):
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    passphrase: str | None = None


class ExchangeSecretOut(BaseModel):
    encrypted_api_key: str
    encrypted_api_secret: str
    encrypted_passphrase: str | None = None


class ExchangeAccountCreate(BaseModel):
    exchange_name: str = Field(min_length=2, max_length=32)
    account_label: str = Field(min_length=1, max_length=64)
    mode: str = Field(default=EXCHANGE_MODE_REAL, min_length=4, max_length=8)
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    passphrase: str | None = None


class ExchangeAccountUpdate(BaseModel):
    account_label: str | None = Field(default=None, min_length=1, max_length=64)
    mode: str | None = Field(default=None, min_length=4, max_length=8)
    api_key: str | None = Field(default=None, min_length=1)
    api_secret: str | None = Field(default=None, min_length=1)
    passphrase: str | None = None


class ExchangeAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    exchange_name: str
    account_label: str
    mode: str
    created_at: datetime
    updated_at: datetime


class ExchangeAccountValidateResponse(BaseModel):
    id: int
    exchange_name: str
    account_label: str
    mode: str
    status: str


class ExchangeAccountsMetaResponse(BaseModel):
    supported_exchanges: list[str]
    supported_modes: list[str]
    default_mode: str


def validate_exchange_name(exchange_name: str) -> str:
    normalized = exchange_name.strip().lower()
    if normalized not in SUPPORTED_EXCHANGES:
        raise ValueError("Unsupported exchange.")
    return normalized


def validate_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in SUPPORTED_EXCHANGE_MODES:
        raise ValueError("Unsupported trading mode.")
    return normalized
