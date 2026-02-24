from dataclasses import dataclass


@dataclass(slots=True)
class ExchangeServiceError(Exception):
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


def error_http_status(code: str) -> int:
    if code in {"not_found"}:
        return 404
    if code in {"authentication_failed"}:
        return 401
    if code in {"insufficient_funds"}:
        return 422
    if code in {"invalid_symbol"}:
        return 400
    if code in {"rate_limited"}:
        return 429
    if code in {"temporary_unavailable"}:
        return 503
    return 400
