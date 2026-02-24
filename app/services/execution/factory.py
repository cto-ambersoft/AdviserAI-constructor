from typing import cast

from app.schemas.exchange_trading import SUPPORTED_EXCHANGES, ExchangeName
from app.services.execution.base import CexAdapter, ExchangeCredentials
from app.services.execution.ccxt_adapter import CcxtAdapter
from app.services.execution.errors import ExchangeServiceError


def normalize_exchange_name(raw_value: str) -> ExchangeName:
    normalized = raw_value.strip().lower()
    if normalized not in SUPPORTED_EXCHANGES:
        raise ExchangeServiceError(
            code="unsupported_exchange",
            message=f"Exchange '{raw_value}' is not supported.",
        )
    return cast(ExchangeName, normalized)


def create_cex_adapter(credentials: ExchangeCredentials) -> CexAdapter:
    exchange_name = normalize_exchange_name(credentials.exchange_name)
    return CcxtAdapter(
        ExchangeCredentials(
            exchange_name=exchange_name,
            api_key=credentials.api_key,
            api_secret=credentials.api_secret,
            mode=credentials.mode,
            passphrase=credentials.passphrase,
        )
    )
