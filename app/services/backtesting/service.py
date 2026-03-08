from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategy import Strategy
from app.services.backtesting.atr_order_block import run_atr_order_block
from app.services.backtesting.grid_bot import run_grid_bot
from app.services.backtesting.intraday_momentum import run_intraday_momentum
from app.services.backtesting.knife_catcher import run_knife_catcher
from app.services.backtesting.portfolio import run_portfolio
from app.services.backtesting.vwap_builder import run_vwap_backtest
from app.services.indicators.engine import calc_indicators
from app.services.market_data.service import MarketDataService


class BacktestingService:
    def __init__(self, market_data: MarketDataService | None = None) -> None:
        self.market_data = market_data or MarketDataService()

    async def load_market_frame(
        self,
        symbol: str,
        timeframe: str,
        bars: int,
        candles: list[dict[str, Any]] | None = None,
    ) -> pd.DataFrame:
        if candles:
            return self.market_data.frame_from_candles(candles)
        return await self.market_data.fetch_ohlcv(
            exchange_name="bybit",
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
        )

    async def run_vwap(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self.load_market_frame(
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            bars=payload["bars"],
            candles=payload.get("candles"),
        )
        indicators = calc_indicators(df)
        return run_vwap_backtest(df, indicators, payload)

    async def run_atr_order_block(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self.load_market_frame(
            payload["symbol"],
            payload["timeframe"],
            payload["bars"],
            payload.get("candles"),
        )
        return run_atr_order_block(df, payload)

    async def run_knife(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self.load_market_frame(
            payload["symbol"],
            payload["timeframe"],
            payload["bars"],
            payload.get("candles"),
        )
        return run_knife_catcher(df, payload)

    async def run_grid(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self.load_market_frame(
            payload["symbol"],
            payload["timeframe"],
            payload["bars"],
            payload.get("candles"),
        )
        return run_grid_bot(df, payload)

    async def run_intraday(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self.load_market_frame(
            payload["symbol"],
            payload["timeframe"],
            payload["bars"],
            payload.get("candles"),
        )
        return run_intraday_momentum(df, payload)

    async def run_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategies = payload.get("strategies", [])
        user_id_raw = payload.get("user_id")
        user_id = int(user_id_raw) if user_id_raw is not None else None
        session = payload.get("session")
        if not strategies and user_id and isinstance(session, AsyncSession):
            strategies = await self._resolve_portfolio_strategies(payload, session=session, user_id=user_id)
        total_capital = float(payload.get("total_capital", 0.0))
        return await run_portfolio(strategies, total_capital, self.market_data)

    async def _resolve_portfolio_strategies(
        self,
        payload: dict[str, Any],
        session: AsyncSession,
        user_id: int,
    ) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        user_items = payload.get("user_strategies", []) or []
        builtin_items = payload.get("builtin_strategies", []) or []

        for item in builtin_items:
            resolved.append(
                {
                    "name": str(item.get("name", "")).strip() or "Builtin Strategy",
                    "weight": float(item.get("allocation_pct", 0.0)),
                    "config": dict(item.get("config", {}) or {}),
                }
            )

        if not user_items:
            return resolved

        strategy_ids = [int(item["strategy_id"]) for item in user_items]
        rows = await session.scalars(
            select(Strategy).where(Strategy.user_id == user_id, Strategy.id.in_(strategy_ids))
        )
        strategy_map = {row.id: row for row in rows.all()}
        missing = [sid for sid in strategy_ids if sid not in strategy_map]
        if missing:
            raise ValueError(f"Strategies not found for user: {missing}")

        for item in user_items:
            strategy_id = int(item["strategy_id"])
            row = strategy_map[strategy_id]
            config = dict(row.config or {})
            config.setdefault("strategy_type", row.strategy_type)
            resolved.append(
                {
                    "name": row.name,
                    "weight": float(item.get("allocation_pct", 0.0)),
                    "config": config,
                }
            )
        return resolved
