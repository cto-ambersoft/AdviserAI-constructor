"""Strategy profile configuration schemas."""

from __future__ import annotations

import math
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

STRATEGY_PROFILE_ADJUSTMENT_KEYS = (
    "watcher",
    "trailing",
    "breakeven",
    "volatility",
)
_ADJUSTMENT_KEY_SET = frozenset(STRATEGY_PROFILE_ADJUSTMENT_KEYS)
_MOVE_SL_TO_TP_RE = re.compile(r"^tp([1-9]\d*)$")


def _validate_inclusive_range(
    *,
    value: float,
    field_name: str,
    lower: float,
    upper: float,
) -> float:
    if lower <= value <= upper:
        return value
    raise ValueError(f"{field_name} must be between {lower} and {upper}.")


class StrategyProfileTPLevel(BaseModel):
    price_offset_pct: float = Field(gt=0)
    close_pct: float = Field(gt=0, le=100.0)
    # Deprecated string form kept for back-compat: "breakeven" or "tpN".
    move_sl_to: str | None = None
    # Preferred: where to place SL on this level's fill, expressed as a
    # signed percentage of the entry→TP interval.
    #
    #   100  → SL right at this TP's price (no drawdown on remaining qty)
    #    50  → SL halfway between entry and TP (lock half of the profit)
    #     0  → SL at entry (breakeven)
    #   -50  → SL halfway between entry and the original SL (loosens risk
    #          relative to breakeven, but tighter than the original SL)
    #  -100  → SL roughly at the position's original SL price (no change)
    #  null  → do not move SL.
    #
    # Negative values are valid because the formula
    #   new_SL = entry + (TP_price − entry) × pct/100
    # extrapolates naturally: a negative ratio places SL on the side opposite
    # to the TP relative to entry. Takes priority over the legacy `move_sl_to`.
    sl_lock_pct: float | None = Field(default=None, ge=-100.0, le=200.0)

    @field_validator("move_sl_to")
    @classmethod
    def validate_move_sl_to(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized == "breakeven":
            return normalized
        if _MOVE_SL_TO_TP_RE.fullmatch(normalized) is not None:
            return normalized
        raise ValueError("move_sl_to must be null, 'breakeven', or a tp reference like 'tp1'.")


class StrategyProfileWatcher(BaseModel):
    indicator: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    condition: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    action_params: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True

    @field_validator("indicator")
    @classmethod
    def normalize_indicator(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized:
            return normalized
        raise ValueError("indicator cannot be empty.")

    @field_validator("condition", "action")
    @classmethod
    def normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized:
            return normalized
        raise ValueError("Value cannot be empty.")


class StrategyProfileConfig(BaseModel):
    sl_mode: Literal["fixed", "atr", "percentage"] = "fixed"
    sl_value: float = Field(gt=0)
    tp_mode: Literal["single", "multi"] = "single"
    tp_value: float | None = Field(default=None, gt=0)
    tp_levels: list[StrategyProfileTPLevel] | None = None
    trailing_enabled: bool = False
    trailing_callback_rate: float = 1.0
    breakeven_enabled: bool = False
    breakeven_trigger_rr: float = 1.0
    volatility_sl_enabled: bool = False
    volatility_atr_period: int = Field(default=14, ge=1)
    volatility_atr_multiplier: float = 2.0
    watchers: list[StrategyProfileWatcher] = Field(default_factory=list)
    adjustment_priority: list[str] = Field(
        default_factory=lambda: list(STRATEGY_PROFILE_ADJUSTMENT_KEYS)
    )
    max_position_pct: float = Field(default=100.0, gt=0, le=100.0)
    allow_sl_widen: bool = False

    @field_validator("trailing_callback_rate")
    @classmethod
    def validate_trailing_callback_rate(cls, value: float) -> float:
        return _validate_inclusive_range(
            value=value,
            field_name="trailing_callback_rate",
            lower=0.1,
            upper=10.0,
        )

    @field_validator("breakeven_trigger_rr")
    @classmethod
    def validate_breakeven_trigger_rr(cls, value: float) -> float:
        return _validate_inclusive_range(
            value=value,
            field_name="breakeven_trigger_rr",
            lower=0.5,
            upper=5.0,
        )

    @field_validator("volatility_atr_multiplier")
    @classmethod
    def validate_volatility_atr_multiplier(cls, value: float) -> float:
        return _validate_inclusive_range(
            value=value,
            field_name="volatility_atr_multiplier",
            lower=0.5,
            upper=5.0,
        )

    @field_validator("adjustment_priority")
    @classmethod
    def validate_adjustment_priority(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        invalid = sorted({item for item in normalized if item not in _ADJUSTMENT_KEY_SET})
        if invalid:
            allowed = ", ".join(STRATEGY_PROFILE_ADJUSTMENT_KEYS)
            unknown = ", ".join(invalid)
            raise ValueError(
                f"adjustment_priority contains invalid keys: {unknown}. Allowed: {allowed}."
            )

        duplicates = [
            item for index, item in enumerate(normalized) if item in normalized[:index]
        ]
        if duplicates:
            repeated = ", ".join(dict.fromkeys(duplicates))
            raise ValueError(f"adjustment_priority must not contain duplicates: {repeated}.")
        return normalized

    @model_validator(mode="after")
    def validate_tp_configuration(self) -> Self:
        if self.tp_mode == "single":
            if self.tp_value is None:
                raise ValueError("tp_value is required when tp_mode='single'.")
            return self

        if not self.tp_levels:
            raise ValueError("tp_levels is required when tp_mode='multi'.")

        total_close_pct = sum(level.close_pct for level in self.tp_levels)
        if not math.isclose(total_close_pct, 100.0, abs_tol=0.1):
            raise ValueError(
                "tp_levels close_pct must sum to 100% (+/- 0.1) when tp_mode='multi'."
            )
        return self


__all__ = [
    "STRATEGY_PROFILE_ADJUSTMENT_KEYS",
    "StrategyProfileConfig",
    "StrategyProfileTPLevel",
    "StrategyProfileWatcher",
]
