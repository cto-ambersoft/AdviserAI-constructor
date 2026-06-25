"""Map ``auto_trade_events`` rows to Telegram HTML messages.

Pure functions (no I/O) so they're trivial to unit-test. The event taxonomy
here is the single source of truth for *which* events are notifiable and which
user toggle (``open`` / ``close`` / ``risk``) gates each one.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

# Event families. Each maps to one user-facing toggle on the settings row.
OPEN_EVENTS: frozenset[str] = frozenset(
    {
        "position_opened",
        "position_synced_open_from_exchange",
    }
)
CLOSE_EVENTS: frozenset[str] = frozenset(
    {
        "position_closed_on_opposite_trend",
        "position_manual_closed",
        "position_reconciled_closed_via_rest",
        "position_marked_closed_from_exchange_state",
        "multi_tp_reconciled_via_rest",
    }
)
RISK_EVENTS: frozenset[str] = frozenset(
    {
        "kill_switch_triggered",
        "strategy_auto_paused",
        "kpi_guard_triggered",
        "position_emergency_closed_unprotected",
        "portfolio_dd_halt",
        # B5 (W10) Promotion Pipeline + B6 (W12) anomaly detection.
        "promotion_ready",
        "strategy_promoted",
        "strategy_demoted",
        "promotion_gate_failed",
        "strategy_anomaly_detected",
    }
)
NOTIFIABLE_EVENTS: frozenset[str] = OPEN_EVENTS | CLOSE_EVENTS | RISK_EVENTS


def toggle_for_event(event_type: str) -> str | None:
    """Return the settings toggle (``open``/``close``/``risk``) gating an event."""
    if event_type in OPEN_EVENTS:
        return "open"
    if event_type in CLOSE_EVENTS:
        return "close"
    if event_type in RISK_EVENTS:
        return "risk"
    return None


def _esc(value: object) -> str:
    # Telegram HTML parse_mode requires &, <, > escaped (not quotes).
    return html.escape(str(value), quote=False)


def _num(value: object) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _esc(value)
    if number == int(number) and abs(number) < 1e15:
        return f"{int(number):,}"
    return f"{number:,.8f}".rstrip("0").rstrip(".")


def _side(value: object) -> str | None:
    if value is None:
        return None
    token = str(value).strip().upper()
    if token in {"LONG", "BUY", "UP"}:
        return "LONG"
    if token in {"SHORT", "SELL", "DOWN"}:
        return "SHORT"
    return None


def _symbol(payload: Mapping[str, Any]) -> str | None:
    for key in ("symbol", "execution_symbol"):
        raw = payload.get(key)
        if raw:
            return str(raw)
    return None


def _humanize(event_type: str) -> str:
    return event_type.replace("_", " ").capitalize()


def _format_open(payload: Mapping[str, Any], message: str | None) -> str:
    symbol = _symbol(payload)
    side = _side(payload.get("trend") or payload.get("side"))
    marker = {"LONG": "🟢", "SHORT": "🔴"}.get(side or "", "🆕")
    header = f"{marker} <b>{side or 'OPEN'} {_esc(symbol or '—')}</b>"
    lines = [header]

    detail: list[str] = []
    if payload.get("entry_price") is not None:
        detail.append(f"Entry {_num(payload.get('entry_price'))}")
    if payload.get("quantity") is not None:
        detail.append(f"Qty {_num(payload.get('quantity'))}")
    if detail:
        lines.append(" · ".join(detail))

    sl_tp: list[str] = []
    if payload.get("sl_price") is not None:
        sl_tp.append(f"SL {_num(payload.get('sl_price'))}")
    if payload.get("tp_price") is not None:
        sl_tp.append(f"TP {_num(payload.get('tp_price'))}")
    if sl_tp:
        lines.append(" · ".join(sl_tp))

    if payload.get("confidence_pct") is not None:
        lines.append(f"Confidence {_num(payload.get('confidence_pct'))}%")
    return "\n".join(lines)


def _format_close(event_type: str, payload: Mapping[str, Any], message: str | None) -> str:
    symbol = _symbol(payload)

    if event_type == "multi_tp_reconciled_via_rest":
        header = f"🎯 <b>Partial TP {_esc(symbol or '')}</b>".rstrip()
        levels = payload.get("advanced_levels")
        lines = [header]
        if isinstance(levels, list) and levels:
            lines.append("TP levels: " + ", ".join(str(int(x) + 1) for x in levels))
        if message:
            lines.append(_esc(message))
        return "\n".join(lines)

    side = _side(payload.get("position_side") or payload.get("side"))
    label = " ".join(part for part in ("Closed", side or "", symbol or "") if part).strip()
    lines = [f"🔻 <b>{_esc(label)}</b>"]

    price = payload.get("close_price") or payload.get("avg_price") or payload.get("exit_price")
    if price is not None:
        lines.append(f"Exit {_num(price)}")

    pnl = payload.get("realized_pnl")
    if pnl is None:
        pnl = payload.get("pnl")
    if pnl is not None:
        lines.append(f"PnL {_num(pnl)}")

    reason = payload.get("close_reason") or payload.get("reason")
    if reason:
        lines.append(f"Reason: {_esc(reason)}")
    elif message:
        lines.append(_esc(message))
    return "\n".join(lines)


def _format_portfolio_dd_halt(payload: Mapping[str, Any], message: str | None) -> str:
    # Invariant: every interpolated value must be numeric via ``_num`` or escaped via
    # ``_esc``. This payload is server-generated numerics only (see service.py); if a
    # string field is ever added here it MUST be wrapped in ``_esc`` — Telegram renders
    # this with parse_mode=HTML, so an unescaped value is a message-injection vector.
    lines = ["🛑 <b>Portfolio drawdown halt</b>"]
    worst = payload.get("worst_dd_pct")
    threshold = payload.get("threshold_pct")
    if worst is not None and threshold is not None:
        # The DD is a historical N-day figure (worst closed-trade drawdown), not live
        # open-position DD — label the window so operators aren't misled.
        window = payload.get("window_days")
        window_txt = f" ({_num(window)}-day)" if window is not None else ""
        dd = f"Worst-strategy drawdown{window_txt} {_num(worst)}%"
        lines.append(f"{dd} ≥ {_num(threshold)}% threshold")
    paused = payload.get("paused_count")
    if paused is not None:
        lines.append(f"Paused {_num(paused)} strategies")
    elif message:
        lines.append(_esc(message))
    return "\n".join(lines)


def _format_risk(event_type: str, payload: Mapping[str, Any], message: str | None) -> str:
    if event_type == "portfolio_dd_halt":
        return _format_portfolio_dd_halt(payload, message)
    lines = [f"⚠️ <b>{_esc(_humanize(event_type))}</b>"]
    symbol = _symbol(payload)
    if symbol:
        lines.append(f"Symbol: {_esc(symbol)}")
    if message:
        lines.append(_esc(message))
    elif payload.get("reason"):
        lines.append(_esc(payload.get("reason")))
    return "\n".join(lines)


def format_event(*, event_type: str, payload: Mapping[str, Any], message: str | None) -> str:
    """Render an event into Telegram HTML. Never raises on missing keys."""
    family = toggle_for_event(event_type)
    if family == "open":
        return _format_open(payload, message)
    if family == "close":
        return _format_close(event_type, payload, message)
    if family == "risk":
        return _format_risk(event_type, payload, message)
    # Not notifiable, but render something safe rather than crash.
    header = f"ℹ️ <b>{_esc(_humanize(event_type))}</b>"
    return f"{header}\n{_esc(message)}" if message else header
