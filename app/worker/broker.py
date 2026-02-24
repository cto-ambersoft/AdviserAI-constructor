from typing import Any

from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.core.config import get_settings

settings = get_settings()
result_backend: Any = RedisAsyncResultBackend(redis_url=settings.redis_url)
broker = RedisStreamBroker(url=settings.redis_url).with_result_backend(result_backend)
