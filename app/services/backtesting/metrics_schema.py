"""Canonical metrics catalogue exposed to the admin UI.

Defines the camelCase keys, human labels and formatting hints for every
metric that may appear in an `ai_forecast_catalogue.metrics` entry or a
backtest summary. Frontend consumes this schema to render tables instead of
hard-coding column definitions.
"""

from typing import Final, Literal

MetricFormat = Literal["percent", "ratio", "money", "integer", "decimal"]

# Each metric entry is a plain dict (dataclass would force JSON conversion).
Metric = dict[str, object]


def _metric(
    key: str,
    label: str,
    *,
    fmt: MetricFormat,
    description: str,
    aliases: tuple[str, ...] = (),
    group: str = "performance",
    direction: Literal["higher_better", "lower_better", "neutral"] = "neutral",
    precision: int | None = None,
) -> Metric:
    metric: Metric = {
        "key": key,
        "label": label,
        "format": fmt,
        "description": description,
        "group": group,
        "direction": direction,
    }
    if aliases:
        metric["aliases"] = list(aliases)
    if precision is not None:
        metric["precision"] = precision
    return metric


GROUPS: Final[list[dict[str, str]]] = [
    {"key": "performance", "label": "Performance"},
    {"key": "risk", "label": "Risk"},
    {"key": "capital", "label": "Capital"},
    {"key": "delta", "label": "AI vs baseline (delta)"},
]

CATALOGUE_METRICS: Final[list[Metric]] = [
    _metric(
        "winRate",
        "Win %",
        fmt="percent",
        description="Share of closed trades with positive R",
        aliases=("win_rate",),
        direction="higher_better",
        precision=1,
    ),
    _metric(
        "profitFactor",
        "PF",
        fmt="ratio",
        description="Sum of winning R divided by absolute sum of losing R",
        aliases=("profit_factor",),
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "sharpeProxy",
        "Sharpe",
        fmt="ratio",
        description="Sharpe-like proxy from per-trade R values",
        aliases=("sharpe_proxy", "sharpe", "sharpe_1"),
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "maxDrawdownPct",
        "Max DD %",
        fmt="percent",
        description="Maximum equity drawdown over the backtest period",
        aliases=("max_drawdown_pct", "max_drawdown", "maxDrawdown"),
        group="risk",
        direction="lower_better",
        precision=2,
    ),
    _metric(
        "annualizedReturnPct",
        "Ann. %",
        fmt="percent",
        description="Annualised return over the backtest period",
        aliases=("annualized_return_pct", "annualized_return"),
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "calmarRatio",
        "Calmar",
        fmt="ratio",
        description="Annualised return divided by max drawdown",
        aliases=("calmar_ratio",),
        group="risk",
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "totalPnl",
        "Total PnL",
        fmt="money",
        description="Total realised PnL in USDT",
        aliases=("total_pnl",),
        group="capital",
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "totalReturnPct",
        "Total %",
        fmt="percent",
        description="Final return as percentage of starting balance",
        aliases=("total_return_pct",),
        group="capital",
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "deltaTotalPnl",
        "Delta PnL",
        fmt="money",
        description="AI total PnL minus baseline total PnL",
        aliases=("delta_total_pnl", "total_pnl_delta"),
        group="delta",
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "deltaCalmarRatio",
        "Delta Calmar",
        fmt="ratio",
        description="AI Calmar minus baseline Calmar",
        aliases=("delta_calmar_ratio", "calmar_ratio_delta"),
        group="delta",
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "deltaWinRate",
        "Delta Win %",
        fmt="percent",
        description="AI win-rate minus baseline win-rate",
        aliases=("delta_win_rate", "win_rate_delta"),
        group="delta",
        direction="higher_better",
        precision=1,
    ),
    _metric(
        "deltaSharpeProxy",
        "Delta Sharpe",
        fmt="ratio",
        description="AI Sharpe proxy minus baseline Sharpe proxy",
        aliases=("delta_sharpe_proxy", "sharpe_proxy_delta"),
        group="delta",
        direction="higher_better",
        precision=2,
    ),
    _metric(
        "deltaMaxDrawdownPct",
        "Delta Max DD",
        fmt="percent",
        description="AI max drawdown minus baseline (negative is better)",
        aliases=("delta_max_drawdown", "max_drawdown_delta"),
        group="delta",
        direction="lower_better",
        precision=2,
    ),
]


METRICS_SCHEMA: Final[dict[str, object]] = {
    "groups": GROUPS,
    "metrics": CATALOGUE_METRICS,
}
