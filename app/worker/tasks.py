from app.services.backtesting.service import BacktestingService
from app.worker.broker import broker

service = BacktestingService()


@broker.task(task_name="app.worker.tasks.calculate_indicators")
async def calculate_indicators(strategy_id: int) -> dict[str, object]:
    return {"strategy_id": strategy_id, "status": "processed"}


@broker.task(task_name="app.worker.tasks.run_portfolio_backtest")
async def run_portfolio_backtest(payload: dict[str, object]) -> dict[str, object]:
    return await service.run_portfolio(payload)
