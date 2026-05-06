import logging

from app.db.session import AsyncSessionFactory
from app.services.auto_trade.service import AutoTradeService
from app.services.auto_trade.trade_sync import ExchangeTradeSyncService
from app.services.backtesting.service import BacktestingService
from app.services.personal_analysis.service import PersonalAnalysisService
from app.services.watchers.service import run_position_watcher_tick
from app.worker.broker import broker

service = BacktestingService()
personal_analysis_service = PersonalAnalysisService()
auto_trade_service = AutoTradeService()
trade_sync_service = ExchangeTradeSyncService()
logger = logging.getLogger(__name__)


def _stats_has_non_zero(stats: dict[str, int], *, keys: tuple[str, ...]) -> bool:
    return any(int(stats.get(key, 0)) > 0 for key in keys)


@broker.task(task_name="app.worker.tasks.calculate_indicators")
async def calculate_indicators(strategy_id: int) -> dict[str, object]:
    return {"strategy_id": strategy_id, "status": "processed"}


@broker.task(task_name="app.worker.tasks.run_portfolio_backtest")
async def run_portfolio_backtest(payload: dict[str, object]) -> dict[str, object]:
    return await service.run_portfolio(payload)


@broker.task(
    task_name="app.worker.tasks.dispatch_due_personal_analysis",
    schedule=[{"cron": "* * * * *", "schedule_id": "personal_dispatch_every_minute"}],
)
async def dispatch_due_personal_analysis() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await personal_analysis_service.dispatch_due_profiles(session=session)
    if _stats_has_non_zero(stats, keys=("triggered", "errors")):
        logger.info(
            "personal_dispatch summary: triggered=%s errors=%s",
            stats.get("triggered", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.poll_personal_analysis_jobs",
    schedule=[{"cron": "* * * * *", "schedule_id": "personal_poll_every_minute"}],
)
async def poll_personal_analysis_jobs() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await personal_analysis_service.poll_pending_jobs(session=session)
    if _stats_has_non_zero(
        stats,
        keys=("polled", "completed", "failed", "retried", "cleanup_pending", "errors"),
    ):
        logger.info(
            (
                "personal_poll summary: polled=%s completed=%s retried=%s failed=%s "
                "cleanup_pending=%s errors=%s"
            ),
            stats.get("polled", 0),
            stats.get("completed", 0),
            stats.get("retried", 0),
            stats.get("failed", 0),
            stats.get("cleanup_pending", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.process_auto_trade_signal_queue",
    schedule=[{"cron": "* * * * *", "schedule_id": "auto_trade_process_every_minute"}],
)
async def process_auto_trade_signal_queue() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await auto_trade_service.process_signal_queue(session=session)
    if _stats_has_non_zero(
        stats,
        keys=("polled", "completed", "skipped", "retried", "dead", "errors"),
    ):
        logger.info(
            (
                "auto_trade_process summary: polled=%s completed=%s skipped=%s "
                "retried=%s dead=%s errors=%s"
            ),
            stats.get("polled", 0),
            stats.get("completed", 0),
            stats.get("skipped", 0),
            stats.get("retried", 0),
            stats.get("dead", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.sync_auto_trade_exchange_trades",
    schedule=[{"cron": "* * * * *", "schedule_id": "auto_trade_trade_sync_every_minute"}],
)
async def sync_auto_trade_exchange_trades() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await trade_sync_service.sync_running_configs(session=session)
    if _stats_has_non_zero(stats, keys=("configs", "synced", "inserted_or_updated", "errors")):
        logger.info(
            (
                "auto_trade_trade_sync summary: configs=%s synced=%s "
                "inserted_or_updated=%s errors=%s"
            ),
            stats.get("configs", 0),
            stats.get("synced", 0),
            stats.get("inserted_or_updated", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(task_name="position_watcher_tick")
async def position_watcher_tick(position_id: str) -> dict[str, object]:
    return await run_position_watcher_tick(position_id)
