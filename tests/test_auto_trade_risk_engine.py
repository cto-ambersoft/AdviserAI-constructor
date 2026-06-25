"""Unit tests for the Pre-Trade Risk Engine (W8).

These exercise the pure decision logic of ``check_pre_trade`` with in-memory
ORM instances — no DB, no exchange. Integration through the live signal
pipeline lives in ``test_auto_trade_service.py``.
"""

from typing import Any, cast

from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_risk_config import AutoTradeRiskConfig
from app.services.auto_trade.risk import RiskDecision, check_pre_trade


def _config(leverage: int) -> AutoTradeConfig:
    # Only ``.leverage`` is read by the leverage rule; other columns stay unset.
    return AutoTradeConfig(leverage=leverage)


def _risk(**kwargs: Any) -> AutoTradeRiskConfig:
    # SQLAlchemy column defaults apply at flush, not __init__, so ``enabled``
    # must be set explicitly on a detached instance.
    kwargs.setdefault("enabled", True)
    return AutoTradeRiskConfig(**kwargs)


async def _check(config: AutoTradeConfig, risk_cfg: AutoTradeRiskConfig | None) -> RiskDecision:
    # These leverage-rule cases never reach the DB-backed rules (leverage fires
    # first, or all later caps are unset), so a null session is safe here.
    return await check_pre_trade(
        session=cast(Any, None),
        config=config,
        risk_cfg=risk_cfg,
        signal=cast(Any, None),
        execution_symbol="BTC/USDT:USDT",
    )


async def test_blocks_when_leverage_exceeds_ceiling() -> None:
    decision = await _check(_config(10), _risk(leverage_ceiling=5))
    assert decision.allowed is False
    assert decision.rule == "leverage"
    assert decision.payload["leverage"] == 10
    assert decision.payload["leverage_ceiling"] == 5
    assert decision.reason


async def test_allows_when_leverage_equal_to_ceiling() -> None:
    # Ceiling is inclusive: leverage == ceiling is allowed.
    decision = await _check(_config(5), _risk(leverage_ceiling=5))
    assert decision.allowed is True


async def test_allows_when_ceiling_unset() -> None:
    decision = await _check(_config(50), _risk(leverage_ceiling=None))
    assert decision.allowed is True


async def test_allows_when_risk_cfg_missing() -> None:
    decision = await _check(_config(50), None)
    assert decision.allowed is True
    assert decision.rule is None


async def test_allows_when_risk_cfg_disabled() -> None:
    # Disabled master switch ⇒ no rule fires, even one that would block.
    decision = await _check(_config(50), _risk(enabled=False, leverage_ceiling=1))
    assert decision.allowed is True


# --- daily loss limit (T1.5) — inputs are passed in by the gate, so the rule
#     logic is pure and needs no DB session. ---


async def _check_daily(
    *,
    risk_cfg: AutoTradeRiskConfig,
    today_realized_pnl_usdt: float,
    account_balance_usdt: float | None = None,
) -> RiskDecision:
    return await check_pre_trade(
        session=cast(Any, None),
        config=_config(1),
        risk_cfg=risk_cfg,
        signal=cast(Any, None),
        execution_symbol="BTC/USDT:USDT",
        today_realized_pnl_usdt=today_realized_pnl_usdt,
        account_balance_usdt=account_balance_usdt,
    )


async def test_daily_loss_usdt_blocks() -> None:
    decision = await _check_daily(
        risk_cfg=_risk(daily_loss_limit_usdt=40.0), today_realized_pnl_usdt=-50.0
    )
    assert decision.allowed is False
    assert decision.rule == "daily_loss"
    assert decision.payload["today_loss_usdt"] == 50.0
    assert decision.payload["daily_loss_limit_usdt"] == 40.0


async def test_daily_loss_usdt_allows_within_limit_and_on_profit() -> None:
    within = await _check_daily(
        risk_cfg=_risk(daily_loss_limit_usdt=40.0), today_realized_pnl_usdt=-30.0
    )
    assert within.allowed is True
    profit = await _check_daily(
        risk_cfg=_risk(daily_loss_limit_usdt=40.0), today_realized_pnl_usdt=120.0
    )
    assert profit.allowed is True


async def test_daily_loss_pct_blocks() -> None:
    decision = await _check_daily(
        risk_cfg=_risk(daily_loss_limit_pct=4.0),
        today_realized_pnl_usdt=-50.0,
        account_balance_usdt=1000.0,
    )
    assert decision.allowed is False
    assert decision.rule == "daily_loss_pct"
    assert decision.payload["today_loss_pct"] == 5.0


async def test_daily_loss_pct_allows_within_limit() -> None:
    decision = await _check_daily(
        risk_cfg=_risk(daily_loss_limit_pct=4.0),
        today_realized_pnl_usdt=-30.0,
        account_balance_usdt=1000.0,
    )
    assert decision.allowed is True


async def test_daily_loss_pct_fails_open_when_balance_missing() -> None:
    # Balance unavailable ⇒ the pct rule cannot be evaluated. Fail-OPEN: allow
    # the trade but surface a non-blocking warning. Never block on a missing
    # balance (SPEC §6.3).
    decision = await _check_daily(
        risk_cfg=_risk(daily_loss_limit_pct=1.0),
        today_realized_pnl_usdt=-50.0,
        account_balance_usdt=None,
    )
    assert decision.allowed is True
    assert decision.warning is not None


async def test_daily_loss_pct_no_warning_when_no_loss_today() -> None:
    # I1 fix — a profitable/flat day with an unavailable balance must NOT warn:
    # the pct rule can only fire on a loss, so a missing balance is irrelevant.
    decision = await _check_daily(
        risk_cfg=_risk(daily_loss_limit_pct=1.0),
        today_realized_pnl_usdt=50.0,
        account_balance_usdt=None,
    )
    assert decision.allowed is True
    assert decision.warning is None


async def test_daily_loss_usdt_enforced_even_without_balance() -> None:
    # The absolute usdt limit does not need a balance, so it still fires.
    decision = await _check_daily(
        risk_cfg=_risk(daily_loss_limit_usdt=40.0, daily_loss_limit_pct=1.0),
        today_realized_pnl_usdt=-50.0,
        account_balance_usdt=None,
    )
    assert decision.allowed is False
    assert decision.rule == "daily_loss"
