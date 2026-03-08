import logging

from app.db.session import AsyncSessionFactory
from app.services.backtesting.service import BacktestingService
from app.services.personal_analysis.service import PersonalAnalysisService
from app.worker.broker import broker

service = BacktestingService()
personal_analysis_service = PersonalAnalysisService()
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
