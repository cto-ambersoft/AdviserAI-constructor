import warnings

from taskiq.schedule_sources import LabelScheduleSource
from taskiq.scheduler.scheduler import TaskiqScheduler
from taskiq_redis import ListRedisScheduleSource
from taskiq_redis.schedule_source import RedisScheduleSource

from app.core.config import get_settings
from app.worker.broker import broker

settings = get_settings()


def _build_watcher_schedule_source() -> ListRedisScheduleSource:
    """Create the current Redis schedule source with one-time legacy migration."""
    source = ListRedisScheduleSource(
        settings.redis_url,
        prefix="watcher-schedule-v2",
        skip_past_schedules=False,
    )

    # Existing watcher schedules may still live under the legacy deprecated source.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legacy_source = RedisScheduleSource(
            settings.redis_url,
            prefix="watcher-schedule",
        )

    return source.with_migrate_from(legacy_source, delete_schedules=True)


watcher_schedule_source = _build_watcher_schedule_source()

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker), watcher_schedule_source],
)
