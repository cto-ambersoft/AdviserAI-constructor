import math
from pathlib import Path
from typing import Any, cast

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.models.strategy import Strategy
from app.schemas.market import MARKET_EXCHANGE_DEFAULT
from app.services.backtesting.ai_forecast import (
    AI_REQUIRED_COLUMNS,
    resolve_ai_risk_multiplier_per_bar,
    resolve_ai_side_locks_per_bar,
    side_allowed,
)
from app.services.backtesting.atr_order_block import run_atr_order_block
from app.services.backtesting.common import (
    add_capital_metrics,
    add_client_summary_fields,
    calculate_performance_metrics,
)
from app.services.backtesting.cost_model import cost_model_from_params
from app.services.backtesting.grid_bot import run_grid_bot
from app.services.backtesting.intraday_momentum import run_intraday_momentum
from app.services.backtesting.knife_catcher import run_knife_catcher
from app.services.backtesting.portfolio import run_portfolio
from app.services.backtesting.run_manifest import build_run_manifest
from app.services.backtesting.vwap_builder import run_vwap_backtest
from app.services.indicators.engine import calc_indicators
from app.services.market_data.service import MarketDataService


class BacktestingService:
    def __init__(self, market_data: MarketDataService | None = None) -> None:
        self.market_data = market_data or MarketDataService()
        settings = get_settings()
        configured_exports_dir = str(settings.ai_forecast_exports_dir).strip()
        if configured_exports_dir:
            configured_path = Path(configured_exports_dir)
            if not configured_path.is_absolute():
                configured_path = Path.cwd() / configured_path
            self._exports_dir = configured_path
        else:
            self._exports_dir = Path(__file__).resolve().parents[3] / "exports"

    def _iter_exports_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        seen: set[str] = set()

        def _add(path: Path) -> None:
            resolved = str(path.resolve(strict=False))
            if resolved in seen:
                return
            seen.add(resolved)
            candidates.append(path)

        _add(self._exports_dir)

        cwd = Path.cwd().resolve()
        for parent in [cwd, *cwd.parents]:
            _add(parent / "exports")

        service_file = Path(__file__).resolve()
        for parent in service_file.parents:
            _add(parent / "exports")

        _add(Path("/app/exports"))
        return candidates

    def _resolve_exports_dir(self) -> Path | None:
        for candidate in self._iter_exports_candidates():
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None

    def _resolve_ai_forecast_file_path(self, file_name: str) -> Path | None:
        for candidate in self._iter_exports_candidates():
            file_path = candidate / file_name
            if file_path.exists() and file_path.is_file() and file_path.suffix.lower() == ".csv":
                return file_path
        return None

    @staticmethod
    def _normalize_regime(value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized == "bull":
            return "Bull"
        if normalized == "bear":
            return "Bear"
        return "Flat"

    async def load_market_frame(
        self,
        exchange_name: str,
        symbol: str,
        timeframe: str,
        bars: int,
        candles: list[dict[str, Any]] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> pd.DataFrame:
        if candles:
            return self.market_data.frame_from_candles(candles)
        return await self.market_data.fetch_ohlcv(
            exchange_name=exchange_name,
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            start_time=start_time,
            end_time=end_time,
        )

    async def _load_market_frame_from_payload(self, payload: dict[str, Any]) -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "exchange_name": str(payload.get("exchange_name", MARKET_EXCHANGE_DEFAULT)),
            "symbol": payload["symbol"],
            "timeframe": payload["timeframe"],
            "bars": payload["bars"],
            "candles": payload.get("candles"),
        }
        if payload.get("start_time") is not None:
            kwargs["start_time"] = payload.get("start_time")
        if payload.get("end_time") is not None:
            kwargs["end_time"] = payload.get("end_time")
        return await self.load_market_frame(**kwargs)

    @staticmethod
    def _attach_run_manifest(
        result: dict[str, Any],
        engine: str,
        df: pd.DataFrame,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach the reproducibility manifest (Finding 7.1/7.2/7.3) to a result.

        The cost model is rebuilt from the same payload the engine consumed, so
        the manifest records exactly the costs that were applied. No-op-safe: it
        only adds a ``run_manifest`` key and never mutates engine output.
        """
        result["run_manifest"] = build_run_manifest(
            engine=engine,
            candles=df,
            cost=cost_model_from_params(payload),
        )
        return result

    async def run_vwap(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self._load_market_frame_from_payload(payload)
        indicators = calc_indicators(df)
        result = run_vwap_backtest(df, indicators, payload)
        return self._attach_run_manifest(result, "vwap", df, payload)

    async def run_vwap_with_ai(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("run_with_ai", False):
            raise ValueError("run_with_ai must be true for comparison run.")
        ai_rows = payload.get("ai_forecast_rows")
        if isinstance(ai_rows, list) and ai_rows:
            return await self.run_vwap_with_ai_rows(payload)
        file_name = str(payload.get("ai_forecast_file", "")).strip()
        if not file_name:
            raise ValueError("ai_forecast_file is required when run_with_ai=true.")
        ai_rows = self._load_ai_forecast_rows(file_name)
        return await self.run_vwap_with_ai_rows({**payload, "ai_forecast_rows": ai_rows})

    async def run_vwap_with_ai_rows(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("run_with_ai", False):
            raise ValueError("run_with_ai must be true for comparison run.")
        ai_rows = payload.get("ai_forecast_rows")
        if not isinstance(ai_rows, list) or len(ai_rows) == 0:
            raise ValueError("ai_forecast_rows are required when run_with_ai=true.")
        df = await self._load_market_frame_from_payload(payload)
        indicators = calc_indicators(df)
        baseline_payload = dict(payload)
        baseline_payload["run_with_ai"] = False
        baseline_payload.pop("ai_forecast_rows", None)
        ai_payload = dict(payload)
        ai_payload["run_with_ai"] = True
        ai_payload["ai_forecast_rows"] = ai_rows

        baseline = run_vwap_backtest(df, indicators, baseline_payload)
        ai_forecast = run_vwap_backtest(df, indicators, ai_payload)
        self._attach_run_manifest(baseline, "vwap", df, baseline_payload)
        self._attach_run_manifest(ai_forecast, "vwap", df, ai_payload)
        return {"baseline": baseline, "ai_forecast": ai_forecast}

    async def list_ai_forecast_backtest_files(self) -> list[dict[str, str]]:
        exports_dir = self._resolve_exports_dir()
        if exports_dir is None:
            return []
        files = sorted(
            (
                path
                for path in exports_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".csv"
            ),
            key=lambda path: path.name.lower(),
        )
        return [
            {
                "file_name": path.name,
                "modified_at_utc": pd.Timestamp(
                    path.stat().st_mtime,
                    unit="s",
                    tz="UTC",
                ).isoformat(),
            }
            for path in files
        ]

    def _load_ai_forecast_rows(self, file_name: str) -> list[dict[str, Any]]:
        if "/" in file_name or "\\" in file_name:
            raise ValueError("ai_forecast_file must be a plain file name from exports.")
        file_path = self._resolve_ai_forecast_file_path(file_name)
        if file_path is None:
            searched_in = [
                str(path.resolve(strict=False))
                for path in self._iter_exports_candidates()
            ]
            raise ValueError(
                "AI forecast file not found: "
                f"{file_name}. Searched in: {', '.join(searched_in)}"
            )
        try:
            frame = pd.read_csv(file_path)
        except Exception as exc:
            raise ValueError(f"Failed to read AI forecast file '{file_name}': {exc}") from exc

        missing = sorted(AI_REQUIRED_COLUMNS - set(frame.columns))
        if missing:
            raise ValueError(
                f"AI forecast file '{file_name}' is missing required columns: {', '.join(missing)}"
            )
        return cast(list[dict[str, Any]], frame.to_dict(orient="records"))

    def _resolve_ai_rows_from_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]] | None:
        rows = payload.get("ai_forecast_rows")
        if isinstance(rows, list) and rows:
            return rows
        file_name = str(payload.get("ai_forecast_file", "")).strip()
        if file_name:
            return self._load_ai_forecast_rows(file_name)
        return None

    def _thresholds_from_payload(self, payload: dict[str, Any]) -> tuple[float, float]:
        bull = payload.get("ai_bull_confidence_threshold")
        bear = payload.get("ai_bear_confidence_threshold")
        return (
            52.0 if bull is None else float(bull),
            52.0 if bear is None else float(bear),
        )

    def _apply_ai_side_lock(
        self,
        result: dict[str, Any],
        df: pd.DataFrame,
        payload: dict[str, Any],
        initial_balance: float,
    ) -> dict[str, Any]:
        if not bool(payload.get("run_with_ai", False)) or not bool(
            payload.get("ai_entry_side_lock", True)
        ):
            return result
        ai_rows = self._resolve_ai_rows_from_payload(payload)
        if not ai_rows:
            return result
        bull_threshold, bear_threshold = self._thresholds_from_payload(payload)
        locks = resolve_ai_side_locks_per_bar(
            market_index=df.index,
            ai_rows=ai_rows,
            bull_threshold=bull_threshold,
            bear_threshold=bear_threshold,
        )
        if not any(lock != "none" for lock in locks):
            return result

        kept: list[dict[str, Any]] = []
        filtered = 0
        for trade in list(result.get("trades", [])):
            side = str(trade.get("side") or trade.get("direction") or "LONG").lower()
            side = "short" if "short" in side else "long"
            entry_time = trade.get("entry_time") or trade.get("entryTime")
            try:
                ts = pd.to_datetime(entry_time, utc=True)
                idx = int(df.index.searchsorted(ts, side="right") - 1)
            except Exception:
                idx = -1
            lock = locks[idx] if 0 <= idx < len(locks) else "none"
            if side_allowed(side, lock):
                kept.append(trade)
            else:
                filtered += 1

        if filtered == 0:
            result = dict(result)
            summary = dict(result.get("summary", {}) or {})
            summary.update(
                {
                    "ai_forecast_applied": True,
                    "ai_entry_side_lock": True,
                    "ai_filtered_trades": 0,
                }
            )
            result["summary"] = add_client_summary_fields(summary)
            result.setdefault("explanations", [])
            result["explanations"] = [
                *list(result.get("explanations", [])),
                {
                    "type": "ai_entry_side_lock",
                    "filtered_trades": 0,
                },
            ]
            return result
        result = dict(result)
        result["trades"] = kept
        summary, equity_curve = add_capital_metrics(
            summary=calculate_performance_metrics(kept),
            trades=kept,
            initial_balance=initial_balance,
            period_start=df.index[0] if len(df.index) else None,
            period_end=df.index[-1] if len(df.index) else None,
        )
        summary["ai_forecast_applied"] = True
        summary["ai_entry_side_lock"] = True
        summary["ai_filtered_trades"] = filtered
        summary = add_client_summary_fields(summary)
        result["summary"] = summary
        chart_points = dict(result.get("chart_points", {}) or {})
        chart_points["equity_curve"] = equity_curve
        result["chart_points"] = chart_points
        result.setdefault("explanations", [])
        result["explanations"] = [
            *list(result.get("explanations", [])),
            {
                "type": "ai_entry_side_lock",
                "filtered_trades": filtered,
            },
        ]
        return result

    def _apply_ai_grid_risk_multiplier(
        self,
        df: pd.DataFrame,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not bool(payload.get("run_with_ai", False)):
            return payload
        ai_rows = self._resolve_ai_rows_from_payload(payload)
        if not ai_rows:
            return payload
        bull_threshold, bear_threshold = self._thresholds_from_payload(payload)
        multipliers = resolve_ai_risk_multiplier_per_bar(
            market_index=df.index,
            ai_rows=ai_rows,
            bull_threshold=bull_threshold,
            bear_threshold=bear_threshold,
        )
        multiplier = float(pd.Series(multipliers).mean()) if multipliers else 1.0
        if math.isclose(multiplier, 1.0):
            return payload
        adjusted = dict(payload)
        for key in ("allocation_usdt", "initial_capital_usdt", "order_size_usdt"):
            if adjusted.get(key) is not None:
                adjusted[key] = float(adjusted[key]) * multiplier
        adjusted["ai_risk_multiplier"] = multiplier
        return adjusted

    async def run_atr_order_block(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self._load_market_frame_from_payload(payload)
        result = run_atr_order_block(df, payload)
        result = self._apply_ai_side_lock(
            result,
            df,
            payload,
            float(payload.get("allocation_usdt", 1000.0)),
        )
        return self._attach_run_manifest(result, "atr_order_block", df, payload)

    async def run_knife(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self._load_market_frame_from_payload(payload)
        result = run_knife_catcher(df, payload)
        result = self._apply_ai_side_lock(
            result,
            df,
            payload,
            float(payload.get("account_balance", 1000.0)),
        )
        return self._attach_run_manifest(result, "knife_catcher", df, payload)

    async def run_grid(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self._load_market_frame_from_payload(payload)
        adjusted_payload = self._apply_ai_grid_risk_multiplier(df, payload)
        result = run_grid_bot(df, adjusted_payload)
        return self._attach_run_manifest(result, "grid_bot", df, adjusted_payload)

    async def run_intraday(self, payload: dict[str, Any]) -> dict[str, Any]:
        df = await self._load_market_frame_from_payload(payload)
        result = run_intraday_momentum(df, payload)
        result = self._apply_ai_side_lock(
            result,
            df,
            payload,
            float(payload.get("allocation_usdt", 1000.0)),
        )
        return self._attach_run_manifest(result, "intraday_momentum", df, payload)

    async def run_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategies = payload.get("strategies", [])
        user_id_raw = payload.get("user_id")
        user_id = int(user_id_raw) if user_id_raw is not None else None
        session = payload.get("session")
        has_split_payload = bool(payload.get("user_strategies")) or bool(
            payload.get("builtin_strategies")
        )
        if not strategies and has_split_payload:
            if user_id is None:
                raise ValueError("user_id is required to resolve portfolio user_strategies.")
            if isinstance(session, AsyncSession):
                strategies = await self._resolve_portfolio_strategies(
                    payload,
                    session=session,
                    user_id=user_id,
                )
            else:
                async with AsyncSessionFactory() as db_session:
                    strategies = await self._resolve_portfolio_strategies(
                        payload,
                        session=db_session,
                        user_id=user_id,
                    )
        total_capital = float(payload.get("total_capital", 0.0))
        if bool(payload.get("run_with_ai", False)):
            baseline = await run_portfolio(
                self._strip_portfolio_ai(strategies),
                total_capital,
                self.market_data,
            )
            result = await run_portfolio(
                self._apply_portfolio_ai_overrides(strategies, payload),
                total_capital,
                self.market_data,
            )
            return {
                "result": result,
                "baseline": baseline,
                "comparison": self._comparison_delta(result, baseline),
            }
        return await run_portfolio(strategies, total_capital, self.market_data)

    @staticmethod
    def _to_float(value: object, default: float = 0.0) -> float:
        try:
            parsed = float(cast(Any, value))
        except (TypeError, ValueError):
            return default
        return parsed if math.isfinite(parsed) else default

    @staticmethod
    def _to_int(value: object, default: int = 0) -> int:
        try:
            return int(cast(Any, value))
        except (TypeError, ValueError):
            return default

    def _comparison_delta(
        self,
        result: dict[str, Any],
        baseline: dict[str, Any],
    ) -> dict[str, float | int]:
        result_summary = result.get("summary", {})
        baseline_summary = baseline.get("summary", {})
        if not isinstance(result_summary, dict):
            result_summary = {}
        if not isinstance(baseline_summary, dict):
            baseline_summary = {}
        return {
            "total_pnl_delta": self._to_float(result_summary.get("total_pnl"))
            - self._to_float(baseline_summary.get("total_pnl")),
            "win_rate_delta": self._to_float(result_summary.get("win_rate"))
            - self._to_float(baseline_summary.get("win_rate")),
            "trades_delta": self._to_int(result_summary.get("total_trades"))
            - self._to_int(baseline_summary.get("total_trades")),
            "profit_factor_delta": self._to_float(result_summary.get("profit_factor"))
            - self._to_float(baseline_summary.get("profit_factor")),
            "sharpe_proxy_delta": self._to_float(result_summary.get("sharpe_proxy"))
            - self._to_float(baseline_summary.get("sharpe_proxy")),
            "max_drawdown_delta": self._to_float(result_summary.get("max_drawdown"))
            - self._to_float(baseline_summary.get("max_drawdown")),
            "calmar_ratio_delta": self._to_float(result_summary.get("calmar_ratio"))
            - self._to_float(baseline_summary.get("calmar_ratio")),
        }

    @staticmethod
    def _portfolio_ai_overrides(payload: dict[str, Any]) -> dict[str, Any]:
        if not bool(payload.get("run_with_ai", False)):
            return {}
        overrides: dict[str, Any] = {"run_with_ai": True}
        for key in (
            "ai_forecast_file",
            "ai_forecast_rows",
            "ai_bull_confidence_threshold",
            "ai_bear_confidence_threshold",
            "ai_entry_side_lock",
        ):
            if payload.get(key) is not None:
                overrides[key] = payload[key]
        return overrides

    def _apply_portfolio_ai_overrides(
        self,
        strategies: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        overrides = self._portfolio_ai_overrides(payload)
        if not overrides:
            return strategies

        resolved: list[dict[str, Any]] = []
        for strategy in strategies:
            item = dict(strategy)
            config = dict(item.get("config", {}) or {})
            config.update(overrides)
            item["config"] = config
            resolved.append(item)
        return resolved

    def _strip_portfolio_ai(self, strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stripped: list[dict[str, Any]] = []
        for strategy in strategies:
            item = dict(strategy)
            config = dict(item.get("config", {}) or {})
            config["run_with_ai"] = False
            for key in (
                "ai_forecast_file",
                "ai_forecast_rows",
                "ai_bull_confidence_threshold",
                "ai_bear_confidence_threshold",
                "ai_entry_side_lock",
            ):
                config.pop(key, None)
            item["config"] = config
            stripped.append(item)
        return stripped

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
