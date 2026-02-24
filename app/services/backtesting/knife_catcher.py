from typing import Any

import pandas as pd


def _range_pct(open_price: float, high: float, low: float) -> float:
    return (high - low) / open_price if open_price else 0.0


def _wick_share(open_price: float, high: float, low: float, close: float) -> float:
    rng = high - low
    if rng <= 0:
        return 0.0
    upper = high - max(open_price, close)
    lower = min(open_price, close) - low
    return max(0.0, upper + lower) / rng


def _calc_signal(
    side: str,
    entry_mode_long: str,
    entry_mode_short: str,
    knife_move_frac: float,
    entry_k_frac: float,
    open_price: float,
    high: float,
    low: float,
) -> dict[str, float] | None:
    if side == "long":
        if entry_mode_long == "OPEN_LOW":
            move = (open_price - low) / open_price if open_price else 0.0
            base, extreme = open_price, low
        else:
            move = (high - low) / high if high else 0.0
            base, extreme = high, low
        if move < knife_move_frac:
            return None
        rng = base - extreme
        if rng <= 0:
            return None
        return {"move": move, "entry": base - rng * entry_k_frac}

    if entry_mode_short == "OPEN_HIGH":
        move = (high - open_price) / open_price if open_price else 0.0
        base, extreme = open_price, high
    else:
        move = (high - low) / low if low else 0.0
        base, extreme = low, high
    if move < knife_move_frac:
        return None
    rng = extreme - base
    if rng <= 0:
        return None
    return {"move": move, "entry": base + rng * entry_k_frac}


def knife_catcher_backtest(
    df: pd.DataFrame,
    side: str = "long",
    entry_mode_long: str = "OPEN_LOW",
    entry_mode_short: str = "OPEN_HIGH",
    knife_move_pct: float = 0.35,
    entry_k_pct: float = 65.0,
    tp_pct: float = 0.45,
    sl_pct: float = 0.35,
    use_max_range_filter: bool = True,
    max_range_pct: float = 1.2,
    use_wick_filter: bool = True,
    max_wick_share_pct: float = 65.0,
    requote_each_candle: bool = True,
    max_requotes: int = 6,
) -> pd.DataFrame:
    knife_move = knife_move_pct / 100.0
    entry_k = entry_k_pct / 100.0
    take = tp_pct / 100.0
    stop = sl_pct / 100.0
    max_range = max_range_pct / 100.0
    max_wick_share = max_wick_share_pct / 100.0
    pending: dict[str, float | int] | None = None
    in_pos = False
    entry = tp = sl = 0.0
    entry_i = -1
    trades: list[dict[str, Any]] = []

    for i in range(0, len(df) - 1):
        row = df.iloc[i]
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        if in_pos:
            exit_reason = None
            exit_price = 0.0
            if side == "long":
                if low <= sl:
                    exit_reason, exit_price = "SL", sl
                elif high >= tp:
                    exit_reason, exit_price = "TP", tp
            else:
                if high >= sl:
                    exit_reason, exit_price = "SL", sl
                elif low <= tp:
                    exit_reason, exit_price = "TP", tp
            if exit_reason is not None:
                if side == "long":
                    pnl_frac = (exit_price - entry) / entry
                else:
                    pnl_frac = (entry - exit_price) / entry
                trades.append(
                    {
                        "side": side.upper(),
                        "entry_i": entry_i,
                        "exit_i": i,
                        "entry_time": df.index[entry_i],
                        "exit_time": df.index[i],
                        "entry": entry,
                        "exit": exit_price,
                        "tp": tp,
                        "sl": sl,
                        "pnl_pct": pnl_frac,
                        "exit_reason": exit_reason,
                    }
                )
                in_pos = False
                pending = None
            continue

        passes = True
        if use_max_range_filter and _range_pct(open_price, high, low) > max_range:
            passes = False
        if (
            passes
            and use_wick_filter
            and _wick_share(open_price, high, low, close) > max_wick_share
        ):
            passes = False

        sig = (
            _calc_signal(
                side=side,
                entry_mode_long=entry_mode_long,
                entry_mode_short=entry_mode_short,
                knife_move_frac=knife_move,
                entry_k_frac=entry_k,
                open_price=open_price,
                high=high,
                low=low,
            )
            if passes
            else None
        )
        if pending is not None:
            if sig is None:
                pending = None
            elif requote_each_candle:
                if int(pending["requotes"]) >= max_requotes:
                    pending = None
                else:
                    pending = {
                        "entry": float(sig["entry"]),
                        "requotes": int(pending["requotes"]) + 1,
                    }
        if pending is None and sig is not None:
            pending = {"entry": float(sig["entry"]), "requotes": 0}

        if pending is None:
            continue
        next_row = df.iloc[i + 1]
        n_hi = float(next_row["high"])
        n_lo = float(next_row["low"])
        price = float(pending["entry"])
        if n_lo <= price <= n_hi:
            entry = price
            entry_i = i + 1
            if side == "long":
                tp, sl = entry * (1.0 + take), entry * (1.0 - stop)
            else:
                tp, sl = entry * (1.0 - take), entry * (1.0 + stop)
            in_pos = True
            pending = None
    return pd.DataFrame(trades)


def run_knife_catcher(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    trades_df = knife_catcher_backtest(
        df=df,
        side=str(params.get("side", "long")),
        entry_mode_long=str(params.get("entry_mode_long", "OPEN_LOW")),
        entry_mode_short=str(params.get("entry_mode_short", "OPEN_HIGH")),
        knife_move_pct=float(params.get("knife_move_pct", 0.35)),
        entry_k_pct=float(params.get("entry_k_pct", 65.0)),
        tp_pct=float(params.get("tp_pct", 0.45)),
        sl_pct=float(params.get("sl_pct", 0.35)),
        use_max_range_filter=bool(params.get("use_max_range_filter", True)),
        max_range_pct=float(params.get("max_range_pct", 1.2)),
        use_wick_filter=bool(params.get("use_wick_filter", True)),
        max_wick_share_pct=float(params.get("max_wick_share_pct", 65.0)),
        requote_each_candle=bool(params.get("requote_each_candle", True)),
        max_requotes=int(params.get("max_requotes", 6)),
    )
    if trades_df.empty:
        summary = {"total_trades": 0, "win_rate": 0.0, "total_return_pct": 0.0}
    else:
        summary = {
            "total_trades": int(len(trades_df)),
            "win_rate": float((trades_df["pnl_pct"] > 0).mean() * 100),
            "total_return_pct": float(trades_df["pnl_pct"].sum() * 100),
        }
    return {
        "summary": summary,
        "trades": trades_df.to_dict(orient="records"),
        "chart_points": {"ohlcv": df.reset_index().to_dict(orient="records")},
        "explanations": [],
    }
