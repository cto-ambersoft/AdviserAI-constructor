"""Pre-Trade Risk Engine (W8).

Pure, deterministic checks evaluated *before* any order is placed. The engine
never opens a trade — it only allows or blocks one. A missing or disabled risk
config is a no-op (fail-safe): the caller's gate then falls through to the
pre-W8 behaviour, so legacy configs are unaffected.

Rules are evaluated cheapest-first and the first violation wins. T1.2 ships the
static ``leverage_ceiling`` rule; T1.3-T1.6 add the position-count, exposure,
daily-loss and conflicting-signal rules (which is why ``session`` and
``signal`` are already part of the signature).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auto_trade_config import AutoTradeConfig
from app.models.auto_trade_position import AutoTradePosition
from app.models.auto_trade_risk_config import AutoTradeRiskConfig

if TYPE_CHECKING:
    from app.services.auto_trade.signal import ParsedAutoTradeSignal

# Local copies of the position literals. Importing POSITION_OPEN / TREND_* from
# the service module would be circular (service → risk → service).
_STATUS_OPEN = "open"
_SIDE_LONG = "LONG"
_SIDE_SHORT = "SHORT"


@dataclass(frozen=True)
class RiskDecision:
    """Outcome of a pre-trade risk evaluation.

    ``allowed=True`` means open the trade. When blocked, ``rule`` names the
    failing limit and ``payload`` carries the actual-vs-threshold values for
    the ``risk_blocked`` audit event.
    """

    allowed: bool
    rule: str | None = None
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Non-blocking advisory: a rule was skipped because its input was
    # unavailable (e.g. pct daily-loss with no sub-account balance). The caller
    # should surface this but still let the trade through (fail-open).
    warning: str | None = None

    @classmethod
    def allow(cls, *, warning: str | None = None) -> RiskDecision:
        return cls(allowed=True, warning=warning)

    @classmethod
    def block(cls, *, rule: str, reason: str, payload: dict[str, Any]) -> RiskDecision:
        return cls(allowed=False, rule=rule, reason=reason, payload=payload)


async def check_pre_trade(
    *,
    session: AsyncSession,
    config: AutoTradeConfig,
    risk_cfg: AutoTradeRiskConfig | None,
    signal: ParsedAutoTradeSignal,
    execution_symbol: str,
    today_realized_pnl_usdt: float = 0.0,
    account_balance_usdt: float | None = None,
) -> RiskDecision:
    """Evaluate every configured pre-trade limit; the first violation wins.

    ``risk_cfg is None`` or ``enabled is False`` → allow (fail-safe). Each rule
    is skipped when its limit is unset (``None``).

    ``today_realized_pnl_usdt`` and ``account_balance_usdt`` are computed by the
    caller (the gate) for the daily-loss rule — the engine stays pure and does
    not fetch balances. ``account_balance_usdt is None`` means the balance was
    unavailable, in which case the percent rule fails open with a warning.
    """
    if risk_cfg is None or not risk_cfg.enabled:
        return RiskDecision.allow()

    # --- leverage ceiling (T1.2) — static, no I/O ---
    ceiling = risk_cfg.leverage_ceiling
    if ceiling is not None and config.leverage > ceiling:
        return RiskDecision.block(
            rule="leverage",
            reason=f"Configured leverage {config.leverage}x exceeds ceiling {ceiling}x.",
            payload={"leverage": int(config.leverage), "leverage_ceiling": int(ceiling)},
        )

    # --- max open positions (T1.3) — portfolio-wide concurrency caps ---
    # Counted across ALL of the user's strategies, not just this config. A
    # per-config count would be pointless: the ``(user_id, account_id) WHERE
    # status='open'`` unique index caps each account at one open position, and
    # this gate only runs when *this* config has none. So the meaningful scope
    # is the user's whole portfolio (and per-symbol = the "anti-duplicate per
    # symbol" guard across strategies).
    #
    # Concurrency (review I7): the count + the subsequent open are not a single
    # transaction, so two signals processed concurrently (different configs /
    # workers) can each see N-1 and both open, overshooting the cap by one. This
    # ±1 tolerance is accepted for W8 — the per-config signal queue serializes
    # the common case, and a hard guarantee would need per-user gate locking
    # (deferred). The same applies to the exposure cap below.
    max_open = risk_cfg.max_open_positions
    max_open_sym = risk_cfg.max_open_positions_per_symbol
    if max_open is not None or max_open_sym is not None:
        open_base = (
            select(func.count())
            .select_from(AutoTradePosition)
            .where(
                AutoTradePosition.user_id == config.user_id,
                AutoTradePosition.status == _STATUS_OPEN,
            )
        )
        if max_open is not None:
            open_count = int(await session.scalar(open_base) or 0)
            if open_count >= max_open:
                return RiskDecision.block(
                    rule="max_open",
                    reason=f"Open positions ({open_count}) reached the limit of {max_open}.",
                    payload={"open_positions": open_count, "max_open_positions": max_open},
                )
        if max_open_sym is not None:
            sym_count = int(
                await session.scalar(open_base.where(AutoTradePosition.symbol == execution_symbol))
                or 0
            )
            if sym_count >= max_open_sym:
                return RiskDecision.block(
                    rule="max_open_per_symbol",
                    reason=(
                        f"Open positions for {execution_symbol} ({sym_count}) reached the "
                        f"per-symbol limit of {max_open_sym}."
                    ),
                    payload={
                        "symbol": execution_symbol,
                        "open_positions_symbol": sym_count,
                        "max_open_positions_per_symbol": max_open_sym,
                    },
                )

    # --- conflicting signal (T1.6) ---
    # ``block_opposite``: refuse an entry that contradicts a position the user
    # already holds on the same symbol elsewhere in the portfolio. A config owns
    # exactly one account, so the conflicting position belongs to another
    # strategy (the same-config opposite-signal flow is handled separately in
    # ``_process_with_open_position`` — not duplicated here). Only ``off`` and
    # ``block_opposite`` exist; the never-implemented ``net``/``replace`` were
    # removed (T13) so the API can't offer a silently-ignored option.
    policy = risk_cfg.conflicting_signal_policy
    if policy == "block_opposite" and signal.trend in (_SIDE_LONG, _SIDE_SHORT):
        opposite = _SIDE_SHORT if signal.trend == _SIDE_LONG else _SIDE_LONG
        has_opposite = bool(
            await session.scalar(
                select(
                    exists().where(
                        AutoTradePosition.user_id == config.user_id,
                        AutoTradePosition.status == _STATUS_OPEN,
                        AutoTradePosition.symbol == execution_symbol,
                        AutoTradePosition.side == opposite,
                    )
                )
            )
        )
        if has_opposite:
            return RiskDecision.block(
                rule="conflicting_signal",
                reason=(
                    f"An open {opposite} position on {execution_symbol} conflicts with this "
                    f"{signal.trend} entry."
                ),
                payload={
                    "symbol": execution_symbol,
                    "intended_side": signal.trend,
                    "open_opposite_side": opposite,
                },
            )

    # --- exposure cap (T1.4) — portfolio-wide posted-margin ceiling ---
    # Σ position_size_usdt over the user's open positions (the margin posted,
    # not leveraged notional) plus the margin this entry would post. The cap is
    # inclusive: projected exposure may equal the cap but not exceed it. Scope
    # is per-user for the same reason as max_open (per-config is degenerate).
    exposure_cap = risk_cfg.exposure_cap_usdt
    if exposure_cap is not None:
        current_exposure = float(
            await session.scalar(
                select(func.coalesce(func.sum(AutoTradePosition.position_size_usdt), 0.0)).where(
                    AutoTradePosition.user_id == config.user_id,
                    AutoTradePosition.status == _STATUS_OPEN,
                )
            )
            or 0.0
        )
        new_size = float(config.position_size_usdt)
        projected = current_exposure + new_size
        if projected > exposure_cap:
            return RiskDecision.block(
                rule="exposure",
                reason=(
                    f"Projected exposure {projected:.2f} USDT "
                    f"(current {current_exposure:.2f} + new {new_size:.2f}) "
                    f"exceeds the cap of {exposure_cap:.2f} USDT."
                ),
                payload={
                    "current_exposure_usdt": current_exposure,
                    "new_position_size_usdt": new_size,
                    "projected_exposure_usdt": projected,
                    "exposure_cap_usdt": exposure_cap,
                },
            )

    # --- daily loss limit (T1.5) — realized loss within the current UTC day ---
    # ``today_realized_pnl_usdt`` is net (profit positive, loss negative); we
    # only act on losses. The absolute usdt limit needs no balance and always
    # fires; the pct limit needs the sub-account balance and fails open (allow
    # + warning) when it is unavailable, so a flaky balance call never blocks a
    # trade. Both limits use ``>=`` — reaching the cap stops further entries.
    daily_warning: str | None = None
    today_loss = max(0.0, -today_realized_pnl_usdt)
    dll_usdt = risk_cfg.daily_loss_limit_usdt
    if dll_usdt is not None and today_loss >= dll_usdt:
        return RiskDecision.block(
            rule="daily_loss",
            reason=f"Today's realized loss {today_loss:.2f} USDT reached the limit {dll_usdt:.2f}.",
            payload={"today_loss_usdt": today_loss, "daily_loss_limit_usdt": dll_usdt},
        )
    dll_pct = risk_cfg.daily_loss_limit_pct
    # Only evaluate the pct rule when there is a loss to measure. With no loss
    # the rule cannot block, so a missing balance is irrelevant (no warning) and
    # the caller need not have fetched it.
    if dll_pct is not None and today_loss > 0:
        if account_balance_usdt is None:
            daily_warning = (
                "daily_loss_limit_pct not evaluated — sub-account balance unavailable; "
                "trade allowed (fail-open)."
            )
        elif account_balance_usdt > 0:
            today_loss_pct = today_loss / account_balance_usdt * 100.0
            if today_loss_pct >= dll_pct:
                return RiskDecision.block(
                    rule="daily_loss_pct",
                    reason=(
                        f"Today's realized loss {today_loss_pct:.2f}% of balance reached the "
                        f"limit {dll_pct:.2f}%."
                    ),
                    payload={
                        "today_loss_usdt": today_loss,
                        "today_loss_pct": today_loss_pct,
                        "account_balance_usdt": account_balance_usdt,
                        "daily_loss_limit_pct": dll_pct,
                    },
                )

    return RiskDecision.allow(warning=daily_warning)
