import logging
from typing import Any

from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.core.config import get_settings

settings = get_settings()

# Keep worker logs readable: suppress per-request and per-message noise.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("taskiq.receiver.receiver").setLevel(logging.WARNING)
logging.getLogger("taskiq.cli.scheduler.run").setLevel(logging.WARNING)

result_backend: Any = RedisAsyncResultBackend(
    redis_url=settings.redis_url,
    keep_results=settings.taskiq_result_keep_results,
    result_ex_time=settings.taskiq_result_ex_time_seconds,
    prefix_str=settings.taskiq_result_key_prefix,
)
broker = RedisStreamBroker(
    url=settings.redis_url,
    maxlen=settings.taskiq_stream_maxlen,
).with_result_backend(result_backend)
