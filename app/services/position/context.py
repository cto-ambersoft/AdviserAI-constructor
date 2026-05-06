"""PositionContext internal dataclass model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
import json
import re
from typing import Any, Optional

from app.services.position.state_machine import PositionState, PositionStateMachine


class PositionSide(str, Enum):
    """Position side, compatible with architecture and exchange adapters."""

    LONG = "long"
    SHORT = "short"
    BOTH = "both"


SUPPORTED_WATCHER_INDICATORS = frozenset(
    {
        "RSI",
        "MACD",
        "ATR",
        "EMA",
        "SMA",
        "EMA_CROSS",
    }
)
_WATCHER_COMPARISON_RE = re.compile(r"^(>=|>|<=|<)\s*(-?\d+(?:\.\d+)?)$")
_WATCHER_BETWEEN_RE = re.compile(
    r"^between\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_WATCHER_CROSS_RE = re.compile(r"^cross_(below|above)$", re.IGNORECASE)


def _parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return value


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _to_iso_string(value: Any, default: Optional[str] = None) -> Optional[str]:
    if value is None:
        return default
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_watcher_condition(condition: str) -> str:
    normalized = " ".join(condition.strip().split())
    if not normalized:
        raise ValueError("Watcher condition cannot be empty")

    comparison_match = _WATCHER_COMPARISON_RE.fullmatch(normalized)
    if comparison_match is not None:
        operator, threshold = comparison_match.groups()
        return f"{operator} {threshold}"

    between_match = _WATCHER_BETWEEN_RE.fullmatch(normalized)
    if between_match is not None:
        lower_raw, upper_raw = between_match.groups()
        if float(lower_raw) >= float(upper_raw):
            raise ValueError("Watcher between condition requires lower bound < upper bound")
        return f"between {lower_raw} {upper_raw}"

    cross_match = _WATCHER_CROSS_RE.fullmatch(normalized)
    if cross_match is not None:
        direction = cross_match.group(1).lower()
        return f"cross_{direction}"

    raise ValueError(
        "Invalid watcher condition format. Use one of: '> 75', 'cross_below', 'between 30 70'."
    )


def _parse_position_state(value: Any) -> PositionState:
    if isinstance(value, PositionState):
        return value
    if value is None:
        return PositionState.PENDING

    raw = str(value).strip()
    if not raw:
        return PositionState.PENDING

    normalized = raw.lower()
    for member in PositionState:
        if member.value == normalized:
            return member

    member = PositionState.__members__.get(raw.upper())
    if member is not None:
        return member
    return PositionState.PENDING


def _parse_position_side(value: Any) -> PositionSide:
    if isinstance(value, PositionSide):
        return value
    if value is None:
        return PositionSide.LONG

    raw = str(value).strip()
    if not raw:
        return PositionSide.LONG

    normalized = raw.lower()
    for member in PositionSide:
        if member.value == normalized:
            return member

    member = PositionSide.__members__.get(raw.upper())
    if member is not None:
        return member
    return PositionSide.LONG


@dataclass
class SLHistoryEntry:
    """SL adjustment history entry."""

    timestamp: str
    old_price: float
    new_price: float
    reason: str
    trigger_source: str
    exchange_order_id: str


@dataclass
class TPHistoryEntry:
    """TP update history entry."""

    timestamp: str
    tp_level: int
    old_price: float
    new_price: float
    reason: str
    close_pct: float
    exchange_order_id: str


@dataclass
class TPLevel:
    """Multi-TP level configuration."""

    level: int
    price_offset_pct: float
    close_pct: float
    trigger_price: float
    status: str
    exchange_order_id: Optional[str]
    # Deprecated string form kept for back-compat: "breakeven" / "tpN".
    move_sl_to: Optional[str] = None
    # Preferred numeric: % of profit locked on this TP fill (entry→TP interval).
    # Takes priority over move_sl_to when set.
    sl_lock_pct: Optional[float] = None

    @staticmethod
    def compute_trigger_price(
        entry_price: float,
        price_offset_pct: float,
        side: PositionSide,
    ) -> float:
        """Compute absolute TP trigger price from entry and offset."""
        if side == PositionSide.SHORT:
            return float(entry_price) * (1.0 - (float(price_offset_pct) / 100.0))
        return float(entry_price) * (1.0 + (float(price_offset_pct) / 100.0))

    @classmethod
    def from_offset(
        cls,
        *,
        level: int,
        price_offset_pct: float,
        close_pct: float,
        entry_price: float,
        side: PositionSide,
        status: str = "pending",
        exchange_order_id: Optional[str] = None,
        move_sl_to: Optional[str] = None,
        sl_lock_pct: Optional[float] = None,
    ) -> "TPLevel":
        """Build TP level with computed trigger_price."""
        return cls(
            level=level,
            price_offset_pct=price_offset_pct,
            close_pct=close_pct,
            trigger_price=cls.compute_trigger_price(entry_price, price_offset_pct, side),
            status=status,
            exchange_order_id=exchange_order_id,
            move_sl_to=move_sl_to,
            sl_lock_pct=sl_lock_pct,
        )

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        entry_price: float,
        side: PositionSide,
    ) -> "TPLevel":
        """Parse TP level from dict and compute trigger if missing."""
        level = _to_int(raw.get("level"), default=0)
        offset_pct = _to_float(raw.get("price_offset_pct"), default=0.0)
        close_pct = _to_float(raw.get("close_pct"), default=0.0)
        trigger = raw.get("trigger_price")
        if trigger is None:
            trigger_price = cls.compute_trigger_price(entry_price, offset_pct, side)
        else:
            trigger_price = _to_float(trigger)
        return cls(
            level=level,
            price_offset_pct=offset_pct,
            close_pct=close_pct,
            trigger_price=trigger_price,
            status=str(raw.get("status", "pending")),
            exchange_order_id=(
                None if raw.get("exchange_order_id") is None else str(raw["exchange_order_id"])
            ),
            move_sl_to=(
                None if raw.get("move_sl_to") is None else str(raw.get("move_sl_to"))
            ),
            sl_lock_pct=(
                None
                if raw.get("sl_lock_pct") is None
                else _to_float(raw.get("sl_lock_pct"))
            ),
        )


@dataclass
class WatcherConfig:
    """Watcher rule configuration."""

    indicator: str
    params: dict[str, Any]
    condition: str
    action: str
    action_params: dict[str, Any]
    is_active: bool

    def __post_init__(self) -> None:
        indicator = str(self.indicator).strip().upper()
        if indicator not in SUPPORTED_WATCHER_INDICATORS:
            allowed = ", ".join(sorted(SUPPORTED_WATCHER_INDICATORS))
            raise ValueError(f"Unsupported watcher indicator '{indicator}'. Allowed: {allowed}.")
        self.indicator = indicator

        if not isinstance(self.params, dict):
            raise ValueError("Watcher params must be a dictionary.")
        self.params = dict(self.params)

        self.condition = _normalize_watcher_condition(str(self.condition))

        action = str(self.action).strip()
        if not action:
            raise ValueError("Watcher action cannot be empty.")
        self.action = action

        if not isinstance(self.action_params, dict):
            raise ValueError("Watcher action_params must be a dictionary.")
        self.action_params = dict(self.action_params)

        self.is_active = _to_bool(self.is_active, default=True)


@dataclass
class PositionContext:
    """Complete position state persisted in DB."""

    # Identity
    position_id: str = ""
    user_id: str = ""
    account_id: str = ""
    exchange: str = ""
    symbol: str = ""

    # State machine
    state: PositionState = PositionState.PENDING
    state_machine: PositionStateMachine = field(
        default_factory=lambda: PositionStateMachine(
            position_id="",
            initial_state=PositionState.PENDING,
        ),
        repr=False,
        compare=False,
    )

    # Entry
    side: PositionSide = PositionSide.LONG
    entry_price: float = 0.0
    original_quantity: float = 0.0
    current_quantity: float = 0.0
    leverage: int = 1

    # Stop Loss
    current_sl_price: float = 0.0
    sl_exchange_order_id: Optional[str] = None
    sl_type: str = "fixed"
    sl_history: list[SLHistoryEntry] = field(default_factory=list)

    # Take Profit
    tp_mode: str = "single"
    tp_levels: list[TPLevel] = field(default_factory=list)
    current_tp_price: Optional[float] = None
    tp_history: list[TPHistoryEntry] = field(default_factory=list)

    # Trailing stop config
    trailing_enabled: bool = False
    trailing_callback_rate: Optional[float] = None
    trailing_activation_price: Optional[float] = None
    trailing_highest_price: Optional[float] = None
    trailing_lowest_price: Optional[float] = None

    # Breakeven config
    breakeven_enabled: bool = False
    breakeven_trigger_rr: float = 1.0
    breakeven_activated: bool = False

    # Volatility SL config
    volatility_sl_enabled: bool = False
    volatility_atr_period: int = 14
    volatility_atr_multiplier: float = 2.0
    volatility_last_atr: Optional[float] = None

    # Watchers
    active_watchers: list[WatcherConfig] = field(default_factory=list)

    # Adjustment priority chain
    adjustment_priority: list[str] = field(
        default_factory=lambda: ["watcher", "trailing", "breakeven", "volatility"]
    )

    # Timing
    opened_at: str = ""
    closed_at: Optional[str] = None
    last_adjusted_at: Optional[str] = None

    # PnL
    realized_pnl: float = 0.0
    commission_total: float = 0.0

    def __post_init__(self) -> None:
        self.state = _parse_position_state(self.state)
        self.side = _parse_position_side(self.side)

        self.sl_history = [
            entry if isinstance(entry, SLHistoryEntry) else SLHistoryEntry(**entry)
            for entry in self.sl_history
            if isinstance(entry, (SLHistoryEntry, dict))
        ]
        self.tp_levels = [
            entry
            if isinstance(entry, TPLevel)
            else TPLevel.from_dict(
                entry,
                entry_price=self.entry_price,
                side=self.side,
            )
            for entry in self.tp_levels
            if isinstance(entry, (TPLevel, dict))
        ]
        self.tp_history = [
            entry if isinstance(entry, TPHistoryEntry) else TPHistoryEntry(**entry)
            for entry in self.tp_history
            if isinstance(entry, (TPHistoryEntry, dict))
        ]
        self.active_watchers = [
            watcher if isinstance(watcher, WatcherConfig) else WatcherConfig(**watcher)
            for watcher in self.active_watchers
            if isinstance(watcher, (WatcherConfig, dict))
        ]

        if not isinstance(self.state_machine, PositionStateMachine):
            self.state_machine = PositionStateMachine(
                position_id=self.position_id,
                initial_state=self.state,
            )
        else:
            self.state_machine.position_id = self.position_id
            self.state_machine.state = self.state

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "PositionContext":
        """Build PositionContext from DB row-like dict with JSON columns."""
        sl_history_raw = _parse_json(row.get("sl_history_json"), default=[])
        tp_levels_raw = _parse_json(row.get("tp_levels_json"), default=[])
        tp_history_raw = _parse_json(row.get("tp_history_json"), default=[])
        trailing_cfg = _parse_json(row.get("trailing_config_json"), default={})
        breakeven_cfg = _parse_json(row.get("breakeven_config_json"), default={})
        volatility_cfg = _parse_json(row.get("volatility_config_json"), default={})
        watchers_raw = _parse_json(row.get("active_watchers_json"), default=[])
        priority_raw = _parse_json(
            row.get("adjustment_priority_json"),
            default=["watcher", "trailing", "breakeven", "volatility"],
        )
        transition_log_raw = _parse_json(row.get("transition_log_json"), default=[])

        state = _parse_position_state(row.get("state"))
        side = _parse_position_side(row.get("side", row.get("position_side")))
        entry_price = _to_float(row.get("entry_price"), default=0.0)

        context = cls(
            position_id=str(row.get("position_id", row.get("id", ""))),
            user_id=str(row.get("user_id", "")),
            account_id=str(row.get("account_id", "")),
            exchange=str(row.get("exchange", row.get("exchange_name", ""))),
            symbol=str(row.get("symbol", "")),
            state=state,
            side=side,
            entry_price=entry_price,
            original_quantity=_to_float(
                row.get("original_quantity", row.get("quantity", 0.0)),
                default=0.0,
            ),
            current_quantity=_to_float(
                row.get("current_quantity", row.get("quantity", 0.0)),
                default=0.0,
            ),
            leverage=_to_int(row.get("leverage"), default=1),
            current_sl_price=_to_float(
                row.get("current_sl_price", row.get("sl_price", 0.0)),
                default=0.0,
            ),
            sl_exchange_order_id=(
                None if row.get("sl_exchange_order_id") is None else str(row["sl_exchange_order_id"])
            ),
            sl_type=str(row.get("sl_type", "fixed")),
            sl_history=[
                SLHistoryEntry(
                    timestamp=str(item.get("timestamp", "")),
                    old_price=_to_float(item.get("old_price"), default=0.0),
                    new_price=_to_float(item.get("new_price"), default=0.0),
                    reason=str(item.get("reason", "")),
                    trigger_source=str(item.get("trigger_source", "")),
                    exchange_order_id=str(item.get("exchange_order_id", "")),
                )
                for item in (sl_history_raw if isinstance(sl_history_raw, list) else [])
                if isinstance(item, dict)
            ],
            tp_mode=str(row.get("tp_mode", "single")),
            tp_levels=[
                TPLevel.from_dict(item, entry_price=entry_price, side=side)
                for item in (tp_levels_raw if isinstance(tp_levels_raw, list) else [])
                if isinstance(item, dict)
            ],
            current_tp_price=(
                None
                if row.get("current_tp_price", row.get("tp_price")) is None
                else _to_float(row.get("current_tp_price", row.get("tp_price")))
            ),
            tp_history=[
                TPHistoryEntry(
                    timestamp=str(item.get("timestamp", "")),
                    tp_level=_to_int(item.get("tp_level"), default=0),
                    old_price=_to_float(item.get("old_price"), default=0.0),
                    new_price=_to_float(item.get("new_price"), default=0.0),
                    reason=str(item.get("reason", "")),
                    close_pct=_to_float(item.get("close_pct"), default=0.0),
                    exchange_order_id=str(item.get("exchange_order_id", "")),
                )
                for item in (tp_history_raw if isinstance(tp_history_raw, list) else [])
                if isinstance(item, dict)
            ],
            trailing_enabled=_to_bool(
                trailing_cfg.get("enabled", row.get("trailing_enabled")),
                default=False,
            ),
            trailing_callback_rate=(
                None
                if trailing_cfg.get("callback_rate", row.get("trailing_callback_rate")) is None
                else _to_float(trailing_cfg.get("callback_rate", row.get("trailing_callback_rate")))
            ),
            trailing_activation_price=(
                None
                if trailing_cfg.get("activation_price", row.get("trailing_activation_price")) is None
                else _to_float(
                    trailing_cfg.get("activation_price", row.get("trailing_activation_price"))
                )
            ),
            trailing_highest_price=(
                None
                if trailing_cfg.get("highest_price", row.get("trailing_highest_price")) is None
                else _to_float(trailing_cfg.get("highest_price", row.get("trailing_highest_price")))
            ),
            trailing_lowest_price=(
                None
                if trailing_cfg.get("lowest_price", row.get("trailing_lowest_price")) is None
                else _to_float(trailing_cfg.get("lowest_price", row.get("trailing_lowest_price")))
            ),
            breakeven_enabled=_to_bool(
                breakeven_cfg.get("enabled", row.get("breakeven_enabled")),
                default=False,
            ),
            breakeven_trigger_rr=_to_float(
                breakeven_cfg.get("trigger_rr", row.get("breakeven_trigger_rr")),
                default=1.0,
            ),
            breakeven_activated=_to_bool(
                breakeven_cfg.get("activated", row.get("breakeven_activated")),
                default=False,
            ),
            volatility_sl_enabled=_to_bool(
                volatility_cfg.get("enabled", row.get("volatility_sl_enabled")),
                default=False,
            ),
            volatility_atr_period=_to_int(
                volatility_cfg.get("atr_period", row.get("volatility_atr_period")),
                default=14,
            ),
            volatility_atr_multiplier=_to_float(
                volatility_cfg.get("atr_multiplier", row.get("volatility_atr_multiplier")),
                default=2.0,
            ),
            volatility_last_atr=(
                None
                if volatility_cfg.get("last_atr", row.get("volatility_last_atr")) is None
                else _to_float(volatility_cfg.get("last_atr", row.get("volatility_last_atr")))
            ),
            active_watchers=[
                WatcherConfig(
                    indicator=str(item.get("indicator", "")),
                    params=item.get("params", {}) if isinstance(item.get("params", {}), dict) else {},
                    condition=str(item.get("condition", "")),
                    action=str(item.get("action", "")),
                    action_params=(
                        item.get("action_params", {})
                        if isinstance(item.get("action_params", {}), dict)
                        else {}
                    ),
                    is_active=_to_bool(item.get("is_active"), default=True),
                )
                for item in (watchers_raw if isinstance(watchers_raw, list) else [])
                if isinstance(item, dict)
            ],
            adjustment_priority=(
                [str(item) for item in priority_raw]
                if isinstance(priority_raw, list)
                else ["watcher", "trailing", "breakeven", "volatility"]
            ),
            opened_at=_to_iso_string(row.get("opened_at"), default="") or "",
            closed_at=_to_iso_string(row.get("closed_at"), default=None),
            last_adjusted_at=_to_iso_string(row.get("last_adjusted_at"), default=None),
            realized_pnl=_to_float(
                row.get("realized_pnl", row.get("realized_pnl_usdt")),
                default=0.0,
            ),
            commission_total=_to_float(
                row.get("commission_total", row.get("commission_total_usdt")),
                default=0.0,
            ),
        )

        if isinstance(transition_log_raw, list):
            context.state_machine._transition_log = [
                item for item in transition_log_raw if isinstance(item, dict)
            ]
        return context

    def to_db_dict(self) -> dict[str, Any]:
        """Serialize PositionContext back into DB-ready dict format."""
        trailing_config = {
            "enabled": self.trailing_enabled,
            "callback_rate": self.trailing_callback_rate,
            "activation_price": self.trailing_activation_price,
            "highest_price": self.trailing_highest_price,
            "lowest_price": self.trailing_lowest_price,
        }
        breakeven_config = {
            "enabled": self.breakeven_enabled,
            "trigger_rr": self.breakeven_trigger_rr,
            "activated": self.breakeven_activated,
        }
        volatility_config = {
            "enabled": self.volatility_sl_enabled,
            "atr_period": self.volatility_atr_period,
            "atr_multiplier": self.volatility_atr_multiplier,
            "last_atr": self.volatility_last_atr,
        }

        return {
            "position_id": self.position_id,
            "user_id": self.user_id,
            "account_id": self.account_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "state": self.state.value,
            "side": self.side.value,
            "entry_price": self.entry_price,
            "original_quantity": self.original_quantity,
            "current_quantity": self.current_quantity,
            "quantity": self.current_quantity,
            "leverage": self.leverage,
            "current_sl_price": self.current_sl_price,
            "sl_price": self.current_sl_price,
            "sl_exchange_order_id": self.sl_exchange_order_id,
            "sl_type": self.sl_type,
            "sl_history_json": [asdict(item) for item in self.sl_history],
            "tp_mode": self.tp_mode,
            "tp_levels_json": [asdict(item) for item in self.tp_levels],
            "current_tp_price": self.current_tp_price,
            "tp_price": self.current_tp_price,
            "tp_history_json": [asdict(item) for item in self.tp_history],
            "trailing_config_json": trailing_config,
            "breakeven_config_json": breakeven_config,
            "volatility_config_json": volatility_config,
            "active_watchers_json": [asdict(item) for item in self.active_watchers],
            "adjustment_priority_json": list(self.adjustment_priority),
            "transition_log_json": self.state_machine.get_transition_log(),
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "last_adjusted_at": self.last_adjusted_at,
            "realized_pnl": self.realized_pnl,
            "commission_total": self.commission_total,
        }
