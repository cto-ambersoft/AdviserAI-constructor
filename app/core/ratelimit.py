"""Fixed-window rate limiting backed by Redis (T5 / S7).

Used to throttle unauthenticated/auth endpoints (login, 2FA login) per source IP
and per account. Fails OPEN on a Redis outage: for the login path, availability is
preferred over strict limiting, consistent with how the rest of the codebase treats
Redis as best-effort. The per-user TOTP lockout (DB-backed) remains the hard control.
"""

import logging

from redis.asyncio import Redis

from app.worker.broker import broker

logger = logging.getLogger(__name__)


def _get_redis_client() -> Redis:
    return Redis(connection_pool=broker.connection_pool)


async def check_rate_limit(key: str, *, limit: int, window_seconds: int) -> bool:
    """Increment the counter for ``key`` and report whether it is still within
    ``limit`` over the current ``window_seconds`` window. Returns ``True`` when the
    action is allowed. Fails OPEN (returns ``True``) if Redis is unreachable.
    """
    try:
        async with _get_redis_client() as redis:
            count = await redis.incr(key)
            if int(count) == 1:
                await redis.expire(key, window_seconds)
            return int(count) <= limit
    except Exception:
        logger.warning("rate-limit check unavailable (Redis); allowing key=%s", key)
        return True
