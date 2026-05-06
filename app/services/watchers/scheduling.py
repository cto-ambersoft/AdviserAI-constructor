"""Dynamic Taskiq scheduling helpers for per-position watcher ticks."""

from __future__ import annotations

from app.services.position.context import PositionContext
from app.services.watchers.service import get_fastest_timeframe, timeframe_to_minutes
from app.worker.scheduler import watcher_schedule_source
from app.worker.tasks import position_watcher_tick


def watcher_schedule_id(position_id: str) -> str:
    return f"position-watcher:{position_id}"


def timeframe_to_cron(timeframe: str) -> str:
    """Map the fastest watcher timeframe to a scheduler cron expression."""
    minutes = timeframe_to_minutes(timeframe)

    if minutes < 60:
        if 60 % minutes != 0:
            raise ValueError(f"Minute timeframe {timeframe!r} cannot be represented as cron step.")
        if minutes == 1:
            return "* * * * *"
        return f"*/{minutes} * * * *"

    if minutes < 60 * 24:
        hours = minutes // 60
        if minutes % 60 != 0:
            raise ValueError(f"Hour timeframe {timeframe!r} is not aligned to whole hours.")
        if hours == 1:
            return "0 * * * *"
        return f"0 */{hours} * * *"

    if minutes == 60 * 24:
        return "0 0 * * *"

    if minutes == 60 * 24 * 7:
        return "0 0 * * 0"

    raise ValueError(f"Unsupported watcher timeframe for cron scheduling: {timeframe!r}")


async def schedule_position_watcher(position: PositionContext) -> str | None:
    """Create or refresh the periodic Taskiq schedule for a position watcher tick."""
    fastest_timeframe = get_fastest_timeframe(position.active_watchers)
    if fastest_timeframe is None:
        return None

    schedule_id = watcher_schedule_id(position.position_id)
    await watcher_schedule_source.delete_schedule(schedule_id)
    await (
        position_watcher_tick.kicker()
        .with_schedule_id(schedule_id)
        .schedule_by_cron(
            watcher_schedule_source,
            timeframe_to_cron(fastest_timeframe),
            position.position_id,
        )
    )
    return schedule_id


async def unschedule_position_watcher(position_id: str) -> None:
    """Remove the periodic watcher tick schedule for a position."""
    await watcher_schedule_source.delete_schedule(watcher_schedule_id(position_id))
