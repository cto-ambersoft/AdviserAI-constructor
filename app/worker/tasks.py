import logging

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.services.auto_trade.income_sync import ExchangeIncomeSyncService
from app.services.auto_trade.service import AutoTradeService
from app.services.auto_trade.trade_sync import ExchangeTradeSyncService
from app.services.backtesting.service import BacktestingService
from app.services.notifications.service import TelegramNotificationService
from app.services.personal_analysis.freshness import sweep_agent_freshness
from app.services.personal_analysis.service import PersonalAnalysisService
from app.services.watchers.service import run_position_watcher_tick
from app.worker.broker import broker

settings = get_settings()

service = BacktestingService()
personal_analysis_service = PersonalAnalysisService()
auto_trade_service = AutoTradeService()
trade_sync_service = ExchangeTradeSyncService()
income_sync_service = ExchangeIncomeSyncService()
telegram_notify_service = TelegramNotificationService()
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


@broker.task(
    task_name="app.worker.tasks.sync_auto_trade_exchange_income",
    schedule=[{"cron": "* * * * *", "schedule_id": "auto_trade_income_sync_every_minute"}],
)
async def sync_auto_trade_exchange_income() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await income_sync_service.sync_running_configs(session=session)
    if _stats_has_non_zero(stats, keys=("configs", "synced", "inserted_or_updated", "errors")):
        logger.info(
            (
                "auto_trade_income_sync summary: configs=%s synced=%s "
                "inserted_or_updated=%s errors=%s"
            ),
            stats.get("configs", 0),
            stats.get("synced", 0),
            stats.get("inserted_or_updated", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.sweep_agent_data_freshness",
    schedule=[{"cron": "0 */4 * * *", "schedule_id": "agent_freshness_every_4h"}],
)
async def sweep_agent_data_freshness() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await sweep_agent_freshness(
            session=session,
            threshold_minutes=settings.agent_freshness_threshold_minutes,
        )
    if _stats_has_non_zero(stats, keys=("stale", "no_data", "events")):
        logger.info(
            "agent_freshness sweep: profiles=%s agents=%s fresh=%s stale=%s no_data=%s events=%s",
            stats.get("profiles", 0),
            stats.get("agents", 0),
            stats.get("fresh", 0),
            stats.get("stale", 0),
            stats.get("no_data", 0),
            stats.get("events", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.evaluate_kpi_guards",
    schedule=[{"cron": "*/5 * * * *", "schedule_id": "kpi_guard_every_5m"}],
)
async def evaluate_kpi_guards() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await auto_trade_service.sweep_kpi_guards(session=session)
    if _stats_has_non_zero(stats, keys=("paused", "errors")):
        logger.info(
            "kpi_guard sweep: evaluated=%s paused=%s errors=%s",
            stats.get("evaluated", 0),
            stats.get("paused", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.sweep_promotion_gates",
    schedule=[{"cron": "*/30 * * * *", "schedule_id": "promotion_gates_every_30m"}],
)
async def sweep_promotion_gates() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await auto_trade_service.sweep_promotion_gates(session=session)
    if _stats_has_non_zero(stats, keys=("ready", "errors")):
        logger.info(
            "promotion gate sweep: evaluated=%s ready=%s errors=%s",
            stats.get("evaluated", 0),
            stats.get("ready", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.sweep_strategy_anomalies",
    schedule=[{"cron": "*/15 * * * *", "schedule_id": "anomaly_sweep_every_15m"}],
)
async def sweep_strategy_anomalies() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await auto_trade_service.sweep_strategy_anomalies(session=session)
    if _stats_has_non_zero(stats, keys=("alerted", "errors")):
        logger.info(
            "anomaly sweep: evaluated=%s alerted=%s errors=%s",
            stats.get("evaluated", 0),
            stats.get("alerted", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.evaluate_portfolio_dd_guards",
    schedule=[{"cron": "*/5 * * * *", "schedule_id": "portfolio_dd_every_5m"}],
)
async def evaluate_portfolio_dd_guards() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await auto_trade_service.sweep_portfolio_dd_guards(session=session)
    if _stats_has_non_zero(stats, keys=("halted", "errors")):
        logger.info(
            "portfolio_dd sweep: users=%s halted=%s errors=%s",
            stats.get("users", 0),
            stats.get("halted", 0),
            stats.get("errors", 0),
        )
    # A per-user error means that user was NOT evaluated this tick — i.e. a breaching
    # portfolio may have gone un-halted. The watcher is best-effort (one user's bad
    # data must not abort the sweep), NOT a guaranteed circuit breaker, so surface
    # this at WARNING for alerting rather than burying it in the info summary.
    if int(stats.get("errors", 0)) > 0:
        logger.warning(
            "portfolio_dd sweep had %s per-user error(s) — those users were not "
            "evaluated this tick (best-effort watcher, not a guaranteed circuit breaker)",
            stats.get("errors", 0),
        )
    return stats


@broker.task(
    task_name="app.worker.tasks.push_portfolio_kpis",
    schedule=[{"cron": "* * * * *", "schedule_id": "portfolio_kpi_every_1m"}],
)
async def push_portfolio_kpis() -> dict[str, int]:
    # T15 (W12g): push live KPI snapshots over SSE so the Live Monitor reads numbers
    # from the stream. Every minute; the frontend keeps a slower poll as a fallback.
    async with AsyncSessionFactory() as session:
        stats = await auto_trade_service.push_portfolio_kpis(session=session)
    if _stats_has_non_zero(stats, keys=("pushed",)):
        logger.debug("portfolio_kpi push: users=%s pushed=%s", stats["users"], stats["pushed"])
    return stats


@broker.task(
    task_name="app.worker.tasks.dispatch_trade_notifications",
    schedule=[{"cron": "* * * * *", "schedule_id": "telegram_notify_every_minute"}],
)
async def dispatch_trade_notifications() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        stats = await telegram_notify_service.dispatch_pending(session=session)
    if _stats_has_non_zero(stats, keys=("sent", "failed", "errors")):
        logger.info(
            "telegram_notify summary: polled=%s sent=%s skipped=%s failed=%s errors=%s",
            stats.get("polled", 0),
            stats.get("sent", 0),
            stats.get("skipped", 0),
            stats.get("failed", 0),
            stats.get("errors", 0),
        )
    return stats


@broker.task(task_name="position_watcher_tick")
async def position_watcher_tick(position_id: str) -> dict[str, object]:
    return await run_position_watcher_tick(position_id)
