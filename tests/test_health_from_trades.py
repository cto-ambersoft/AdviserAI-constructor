"""Pure tests for health_from_trades (P4-4 slice 2 refactor).

The trades→StrategyHealth core is now shared by live health (positions) and the
promotion-pipeline sandbox validation (backtest trades).
"""

from __future__ import annotations

from app.services.auto_trade.health import (
    HEALTH_CLASS_INSUFFICIENT,
    HEALTH_MIN_TRADES,
    health_from_trades,
)


def _trades(wins: int, losses: int) -> list[dict[str, object]]:
    # win_rate is driven by the R-multiple (pnl_usdt / risk_usdt), so each trade
    # carries an explicit risk so compute_trade_r_multiple yields a finite R.
    return [{"pnl_usdt": 10.0, "risk_usdt": 5.0} for _ in range(wins)] + [
        {"pnl_usdt": -5.0, "risk_usdt": 5.0} for _ in range(losses)
    ]


def test_insufficient_sample_returns_insufficient_data() -> None:
    health = health_from_trades(
        config_id=1, trades=_trades(3, 1), initial_balance=1000.0
    )
    assert health.health_class == HEALTH_CLASS_INSUFFICIENT
    assert health.sample_size == 4
    assert health.win_rate_pct == 0.0  # zeroed on an unreliable sample


def test_reliable_sample_computes_win_rate_and_pnl() -> None:
    wins, losses = 8, 4
    assert wins + losses >= HEALTH_MIN_TRADES
    health = health_from_trades(
        config_id=1, trades=_trades(wins, losses), initial_balance=1000.0
    )
    assert health.health_class != HEALTH_CLASS_INSUFFICIENT
    assert health.sample_size == 12
    assert round(health.win_rate_pct, 1) == round(wins / (wins + losses) * 100, 1)
    assert health.total_pnl_usdt == wins * 10.0 + losses * -5.0


def test_empty_trades_is_insufficient() -> None:
    health = health_from_trades(config_id=1, trades=[], initial_balance=1000.0)
    assert health.health_class == HEALTH_CLASS_INSUFFICIENT
    assert health.sample_size == 0
